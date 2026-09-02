"""
ML Recovery Policy (Phase 5).

Same contract as policy_engine.decide(): takes a DIAGNOSED case, returns a
recorded Decision, leaves the case in whatever state ACTION_TO_STATE says
for the chosen action. Everything about *how* a decision gets enforced and
recorded is reused from policy_engine — guardrail checks, the stopping
rule, ACTION_TO_STATE, record_decision. The only thing this module adds is
*how the action gets chosen*: instead of a fixed ranked list per category,
every guardrail-allowed action gets scored by expected value
(P(recovery) * amount - cost) and the highest-scoring one wins.

This is not wired into the live webhook path (orchestrator.py still calls
policy_engine.decide, not this). Per the plan: "If ML isn't ready ... your
rules engine remains completely functional. That's intentional." This
module exists to be evaluated against the baseline (see
scripts/evaluate_policies.py) and to be switched in deliberately later,
not to silently replace the safety-rail policy that's actually live.

Falls back to the baseline's own fallback (ESCALATE) if the model isn't
trained yet, rather than raising into the caller — a missing model
shouldn't be able to break the pipeline this plugs into.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RevenueCase, Decision
from app.services import policy_engine
from app.services.policy_engine import GuardrailConfig, DEFAULT_GUARDRAILS, ALL_ACTIONS
from app.ml import scorer


def _build_context(db: Session, case: RevenueCase, now: datetime) -> dict:
    previous_contacts = len(policy_engine.contacts_for_case(db, case))
    days_overdue = max(0.0, (now - case.created_at).total_seconds() / 86400) if case.created_at else 0.0
    return {
        "category": case.failure_category,
        "amount": float(case.amount) if case.amount is not None else 0.0,
        "days_overdue": days_overdue,
        "previous_contacts": previous_contacts,
        "subscription_linked": bool(case.razorpay_subscription_id),
        "hour": now.hour,
        "day_of_week": now.weekday(),
    }


def decide_ml(
    db: Session, case: RevenueCase, config: GuardrailConfig = DEFAULT_GUARDRAILS, now: datetime | None = None,
) -> Decision | None:
    if case.state != "DIAGNOSED":
        return None

    now = now or datetime.utcnow()
    case.state = "DECISION_READY"

    if policy_engine.attempt_count(db, case) > config.max_total_attempts:
        return policy_engine.record_decision(
            db, case, chosen_action="STOP",
            alternatives={"STOP": {"allowed": True, "reason": "max_total_attempts stopping rule triggered"}},
            explanation=(
                f"ML policy: case classified as {case.failure_category}. Stopped: exceeded "
                f"max_total_attempts ({config.max_total_attempts})."
            ),
            config=config, now=now,
        )

    context = _build_context(db, case, now)

    allowed_actions = []
    alternatives = {}
    for action in ALL_ACTIONS:
        allowed, block_reason = policy_engine.check_action_allowed(db, case, action, config, now)
        if allowed:
            allowed_actions.append(action)
        else:
            alternatives[action] = {"allowed": False, "reason": block_reason}

    try:
        ranked = scorer.rank_actions(context, allowed_actions) if allowed_actions else []
    except scorer.ModelNotTrainedError:
        ranked = []

    for r in ranked:
        alternatives[r["action"]] = {
            "allowed": True,
            "p_recovery": round(r["p_recovery"], 4),
            "cost": r["cost"],
            "expected_value": round(r["expected_value"], 2),
        }

    if ranked:
        top = ranked[0]
        chosen_action = top["action"]
        explanation = (
            f"ML policy: case classified as {case.failure_category}. Chose {chosen_action} "
            f"— P(recovery)={top['p_recovery']:.2f}, expected value ₹{top['expected_value']:.2f} "
            f"among {len(ranked)} allowed action(s)."
        )
    else:
        # Every candidate blocked by a guardrail, or the model isn't
        # trained yet — fall back to human review, same as the baseline.
        chosen_action = "ESCALATE"
        alternatives.setdefault("ESCALATE", {"allowed": True, "reason": "fallback — no scorable allowed action"})
        explanation = (
            f"ML policy: case classified as {case.failure_category}. Fell back to ESCALATE — "
            f"{'no allowed actions passed guardrails' if not allowed_actions else 'model not trained'}."
        )

    return policy_engine.record_decision(db, case, chosen_action, alternatives, explanation, config, now)
