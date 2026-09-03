"""
Central policy dispatch — one place that maps RECOVERY_POLICY to behavior.

RECOVERY_POLICY:
  baseline — deterministic baseline (default, safe)
  shadow   — baseline executes; adaptive recommendation audited side-effect-free
  adaptive — friction-aware adaptive with baseline fallback
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.time import utc_now
from app.models import AuditEvent, Decision, RevenueCase
from app.services import policy_engine
from app.services.policy_engine import GuardrailConfig, DEFAULT_GUARDRAILS


def _get_policy_mode() -> str:
    mode = (settings.RECOVERY_POLICY or "baseline").strip().lower()
    if mode not in ("baseline", "shadow", "adaptive"):
        return "baseline"
    return mode


def decide_for_case(
    db: Session,
    case: RevenueCase,
    config: GuardrailConfig = DEFAULT_GUARDRAILS,
    now: datetime | None = None,
) -> Decision | None:
    now = now or utc_now()
    mode = _get_policy_mode()

    if mode == "baseline":
        decision = policy_engine.decide(db, case, config=config, now=now)
        if decision is not None:
            try:
                decision.policy_mode = "baseline"
                db.flush()
            except Exception:
                pass
        return decision

    if mode == "shadow":
        return _decide_shadow(db, case, config=config, now=now)

    if mode == "adaptive":
        return _decide_adaptive(db, case, config=config, now=now)

    decision = policy_engine.decide(db, case, config=config, now=now)
    if decision is not None:
        try:
            decision.policy_mode = mode
            db.flush()
        except Exception:
            pass
    return decision


def _decide_shadow(
    db: Session,
    case: RevenueCase,
    config: GuardrailConfig,
    now: datetime,
) -> Decision | None:
    # 1) Side-effect-free adaptive recommendation using PRE-decision state.
    #    Must be computed BEFORE baseline mutates DB (otherwise baseline's
    #    newly created Action would contaminate previous_contacts/friction).
    recommendation = None
    try:
        from app.services import ml_policy
        from app.ml import scorer

        # Case is still DIAGNOSED here; shadow scoring uses the same DB
        # snapshot the baseline will see (no new Action yet).
        recommendation = ml_policy.shadow_recommend(db, case, config=config, now=now)
    except Exception as exc:
        # Shadow scoring must never break baseline path — record error audit
        # but still proceed to baseline execution.
        db.add(AuditEvent(
            revenue_case_id=case.id,
            event="shadow_adaptive_error",
            detail={"error": str(exc)[:300]},
        ))
        db.flush()

    # 2) Authoritative baseline execution — creates Decision + Action
    baseline_decision = policy_engine.decide(db, case, config=config, now=now)
    if baseline_decision is None:
        return None
    try:
        baseline_decision.policy_mode = "shadow"
        baseline_decision.friction_profile = "shadow-baseline-executed"
        db.flush()
    except Exception:
        pass

    # 3) Persist shadow audit comparing the two, if recommendation succeeded
    if recommendation is not None and not recommendation.get("error"):
        try:
            from app.ml import scorer as scorer2

            model_info = scorer2.get_model_info()
            detail = {
                "baseline_chosen": baseline_decision.chosen_action,
                "adaptive_suggested": recommendation.get("suggested_action"),
                "adaptive_utility": recommendation.get("top_utility"),
                "model_version": model_info.get("model_version"),
                "fingerprint": recommendation.get("fingerprint") or model_info.get("fingerprint_short"),
                "friction_profile": recommendation.get("friction_profile"),
                "candidates": recommendation.get("candidates"),
                "disagreement": (baseline_decision.chosen_action != recommendation.get("suggested_action")),
            }
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="shadow_adaptive_recommendation",
                detail=detail,
            ))
            db.flush()
        except Exception as exc:
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="shadow_adaptive_error",
                detail={"error": str(exc)[:300]},
            ))
            db.flush()
    return baseline_decision


def _decide_adaptive(
    db: Session,
    case: RevenueCase,
    config: GuardrailConfig,
    now: datetime,
) -> Decision | None:
    try:
        from app.services import ml_policy
        from app.ml import scorer as _scorer

        decision = ml_policy.decide_ml(db, case, config=config, now=now)
        if decision is not None:
            return decision
    except _scorer.ModelNotTrainedError as exc:
        # Only model/artifact/feature/probability failures fall back — DB
        # errors must propagate, not be swallowed as baseline.
        try:
            if case.state == "DECISION_READY":
                case.state = "DIAGNOSED"
        except Exception:
            pass
        db.add(AuditEvent(
            revenue_case_id=case.id,
            event="adaptive_fallback",
            detail={"reason": str(exc)[:300], "policy_mode": "adaptive", "model_error": True},
        ))
        fallback = policy_engine.decide(db, case, config=config, now=now)
        if fallback is not None:
            try:
                fallback.policy_mode = "adaptive_fallback"
                db.flush()
            except Exception:
                pass
        return fallback
    except Exception:
        # Non-model DB/persistence failures must not be converted to fallback
        raise

    # ml_policy returned None (e.g. not DIAGNOSED)
    if case.state in ("DIAGNOSED", "DECISION_READY"):
        # This should not happen for DIAGNOSED (ml_policy would have returned a decision
        # or raised), but handle gracefully without double fallback audit.
        return None
    return None
