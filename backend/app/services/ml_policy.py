"""
ML Recovery Policy — friction-aware adaptive decisioning.

Same contract as policy_engine.decide(): takes a DIAGNOSED case, returns a
recorded Decision, leaves the case in whatever state ACTION_TO_STATE says
for the chosen action. Everything about *how* a decision gets enforced and
recorded is reused from policy_engine — guardrail checks, the stopping
rule, ACTION_TO_STATE, record_decision. Adaptive scoring is layered on top:

  utility(action) = P(recovery|context,action)*amount - cost(action)
                    - friction_weight * friction_score(action,case)

Friction is explicit, deterministic, and not learned from synthetic labels.
Guardrails remain authoritative; ML only ranks allowed actions.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.time import utc_now
from app.models import AuditEvent, Decision, RevenueCase
from app.services import policy_engine
from app.services.policy_engine import GuardrailConfig, DEFAULT_GUARDRAILS, ALL_ACTIONS
from app.ml import scorer
from app.ml.features import build_live_features
from app.ml.friction import compute_friction_score, compute_utility, get_friction_weight

# Semantic feasibility — adaptive should not choose nonsense like
# WAIT_FOR_NATIVE_RETRY for a non-subscription failure.
WAIT_NATIVE_ACTIONS = {"WAIT_FOR_NATIVE_RETRY"}


def _build_context(db: Session, case: RevenueCase, now: datetime) -> dict:
    # Canonical feature builder — same as scorer/train parity
    row = build_live_features(db, case, "WAIT", now)  # action placeholder, will be replaced per candidate
    # Map to scorer's expected context keys
    return {
        "category": row["failure_category"],
        "amount": row["amount"],
        "days_overdue": row["days_overdue"],
        "previous_contacts": row["previous_contacts"],
        "subscription_linked": row["subscription_linked"],
        "hour": row["hour"],
        "day_of_week": row["day_of_week"],
    }


def _feasible_actions(
    db: Session, case: RevenueCase, allowed: list[str], now: datetime
) -> list[str]:
    """Filter guardrail-allowed actions to semantically valid set."""
    feasible = []
    context = _build_context(db, case, now)
    subscription_linked = context["subscription_linked"]
    for action in allowed:
        if action in WAIT_NATIVE_ACTIONS and not subscription_linked and case.failure_category != "SUBSCRIPTION_PENDING_NATIVE_RETRY":
            continue
        feasible.append(action)
    return feasible if feasible else allowed  # if filter removes everything, fall back to allowed


def _adaptive_choose(
    db: Session,
    case: RevenueCase,
    config: GuardrailConfig,
    now: datetime,
) -> tuple[str, dict, str, dict]:
    """Core ranking: returns (chosen_action, alternatives, explanation, provenance)."""
    try:
        context = _build_context(db, case, now)
    except (ValueError, TypeError, KeyError) as exc:
        raise scorer.ModelNotTrainedError(f"feature build failed: {exc}") from exc

    allowed_actions = []
    alternatives: dict = {}
    for action in ALL_ACTIONS:
        allowed, block_reason = policy_engine.check_action_allowed(db, case, action, config, now)
        if allowed:
            allowed_actions.append(action)
        else:
            alternatives[action] = {"allowed": False, "reason": block_reason}

    # Semantic filter after guardrails
    feasible = _feasible_actions(db, case, allowed_actions, now)
    # If semantic filter removed some, mark them as not feasible
    removed = set(allowed_actions) - set(feasible)
    for action in removed:
        alternatives[action] = {"allowed": False, "reason": "semantically infeasible for this case (e.g. WAIT_FOR_NATIVE_RETRY without subscription)"}
    allowed_actions = feasible

    profile, weight = get_friction_weight(settings.ADAPTIVE_POLICY_PROFILE, settings.ADAPTIVE_FRICTION_WEIGHT)
    # Build friction scores deterministically
    friction_scores = {action: compute_friction_score(action, case, db) for action in allowed_actions}

    try:
        ranked = scorer.rank_actions_with_friction(context, allowed_actions, friction_scores, weight) if allowed_actions else []
    except (ValueError, TypeError, KeyError) as exc:
        # Malformed feature row / invalid probability — treat as model-domain failure
        raise scorer.ModelNotTrainedError(f"feature validation failed: {exc}") from exc

    for r in ranked:
        alternatives[r["action"]] = {
            "allowed": True,
            "p_recovery": round(r["p_recovery"], 4),
            "cost": r["cost"],
            "expected_value": round(r["expected_value"], 2),
            "friction_score": round(r["friction_score"], 2),
            "friction_weight": r["friction_weight"],
            "utility": round(r["utility"], 2),
            "expected_recovered_value": round(r["expected_recovered_value"], 2),
        }

    model_info = scorer.get_model_info()
    provenance = {
        "policy_mode": "adaptive",
        "model_version": model_info.get("model_version"),
        "fingerprint": model_info.get("fingerprint_short"),
        "friction_profile": profile,
        "friction_weight": weight,
    }

    if ranked:
        top = ranked[0]
        chosen_action = top["action"]
        explanation = (
            f"Adaptive policy ({profile}, {model_info.get('model_version') or 'no-model'} {model_info.get('fingerprint_short') or ''}): "
            f"case {case.failure_category}. Chose {chosen_action} — "
            f"P={top['p_recovery']:.2f}, EV ₹{top['expected_value']:.2f}, "
            f"friction {top['friction_score']:.1f}×{weight} → utility ₹{top['utility']:.2f} "
            f"among {len(ranked)} allowed."
        )
        return chosen_action, alternatives, explanation, {**provenance, "top_utility": top["utility"], "top_expected_value": top["expected_value"]}
    else:
        chosen_action = "ESCALATE"
        alternatives.setdefault("ESCALATE", {"allowed": True, "reason": "fallback — no scorable allowed action"})
        explanation = (
            f"Adaptive policy ({profile}): fell back to ESCALATE — "
            f"{'no allowed actions passed guardrails' if not allowed_actions else 'model not trained'}."
        )
        return chosen_action, alternatives, explanation, provenance


def shadow_recommend(
    db: Session,
    case: RevenueCase,
    baseline_decision: Decision | None = None,
    config: GuardrailConfig = DEFAULT_GUARDRAILS,
    now: datetime | None = None,
) -> dict | None:
    """Side-effect-free adaptive recommendation for shadow mode.

    Evaluates the SAME pre-decision state that baseline will see (case is
    DIAGNOSED, no new Action yet). Must not create Decision/Action or mutate
    persistent state. `baseline_decision` is accepted for backward compat but
    ignored — shadow always scores the pre-baseline snapshot.
    """
    # Handle calls where config was passed positionally as baseline_decision
    if isinstance(baseline_decision, GuardrailConfig):
        config = baseline_decision
        baseline_decision = None
    # Also handle count where caller did shadow_recommend(db, case, config=config, now=now)
    # in which case baseline_decision is actually config value via positional
    now = now or utc_now()
    # Case should be DIAGNOSED here (called before baseline). If called
    # after baseline for backward compat, temporarily restore DIAGNOSED.
    original_state = case.state
    needs_restore = original_state != "DIAGNOSED"
    try:
        if needs_restore:
            case.state = "DIAGNOSED"
        chosen, alternatives, explanation, provenance = _adaptive_choose(db, case, config, now)
    except Exception as exc:
        err_provenance = locals().get("provenance", {})
        return {"suggested_action": None, "top_utility": None, "candidates": None, "error": str(exc)[:300], **err_provenance}
    finally:
        if needs_restore:
            case.state = original_state
    # Don't write alternatives to DB; return them
    top = next((v for k, v in alternatives.items() if k == chosen), None)
    return {
        "suggested_action": chosen,
        "top_utility": top.get("utility") if top else None,
        "candidates": alternatives,
        "provenance": provenance,
        "fingerprint": provenance.get("fingerprint"),
        "friction_profile": provenance.get("friction_profile"),
    }


def decide_ml(
    db: Session, case: RevenueCase, config: GuardrailConfig = DEFAULT_GUARDRAILS, now: datetime | None = None,
) -> Decision | None:
    """Friction-aware adaptive decision — live path with fallback."""
    if case.state != "DIAGNOSED":
        return None

    now = now or utc_now()
    case.state = "DECISION_READY"

    if policy_engine.attempt_count(db, case) > config.max_total_attempts:
        decision = policy_engine.record_decision(
            db, case, chosen_action="STOP",
            alternatives={"STOP": {"allowed": True, "reason": "max_total_attempts stopping rule triggered"}},
            explanation=(
                f"ML policy: case classified as {case.failure_category}. Stopped: exceeded "
                f"max_total_attempts ({config.max_total_attempts})."
            ),
            config=config, now=now,
        )
        try:
            decision.policy_mode = "adaptive"
            decision.friction_profile = settings.ADAPTIVE_POLICY_PROFILE
            db.flush()
        except Exception:
            pass
        return decision

    # Adaptive scoring — any model/artifact/feature/probability failure
    # is raised for the dispatcher to own fallback (single owner).
    try:
        chosen_action, alternatives, explanation, provenance = _adaptive_choose(db, case, config, now)
    except Exception as exc:
        # Restore safe state and re-raise for dispatcher — do NOT emit
        # adaptive_fallback here (dispatcher is the single owner).
        if case.state == "DECISION_READY":
            case.state = "DIAGNOSED"
        raise
    decision = policy_engine.record_decision(
        db, case, chosen_action, alternatives, explanation, config, now,
        expected_value=provenance.get("top_expected_value"),
    )
    if decision.alternatives is None:
        decision.alternatives = {}
    decision.alternatives["_provenance"] = provenance
    decision.policy_mode = "adaptive"
    decision.model_version = provenance.get("model_version")
    decision.model_fingerprint = provenance.get("fingerprint")
    decision.friction_profile = provenance.get("friction_profile")
    try:
        decision.friction_weight = provenance.get("friction_weight")
    except Exception:
        pass
    db.flush()
    db.add(AuditEvent(
        revenue_case_id=case.id,
        event="adaptive_decision",
        detail=provenance,
    ))
    return decision
