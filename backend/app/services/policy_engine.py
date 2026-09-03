"""
Guardrail Engine + Recovery Policy Engine (Phase 3).

This is the deterministic "baseline policy" the plan calls for — the thing
Phase 5's ML model will later have to beat. It has to exist and be correct
on its own before any prediction gets layered on top of it, and it doubles
as the safety rail an ML-chosen action still has to pass through later.

Order of operations, every time a case is diagnosed:

    stopping rule check (too many attempts already? -> STOP)
              |
              v
    candidate actions (ranked by failure category)
              |
              v
    guardrail filter (contact limits, cooldown, amount threshold)
              |
              v
    first allowed candidate wins -> Decision + Action recorded
              |
              v
    case.state updated to match the chosen action

No LLM and no side effects. This module only decides and records; the
temporal worker claims executable actions after the transaction commits.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import RevenueCase, Decision, Action, AuditEvent
from app.core.config import settings
from app.core.time import utc_now

CONTACT_ACTIONS = {"CONTACT_CUSTOMER", "CREATE_PAYMENT_LINK", "COLLECT_PROMISE_TO_PAY"}
WAIT_ACTIONS = {"WAIT", "WAIT_FOR_NATIVE_RETRY"}

# Every state this engine can leave a case in, keyed by the action chosen.
ACTION_TO_STATE = {
    "WAIT": "WAITING",
    "WAIT_FOR_NATIVE_RETRY": "WAITING",
    "CREATE_PAYMENT_LINK": "ACTION_SCHEDULED",
    "CONTACT_CUSTOMER": "ACTION_SCHEDULED",
    "COLLECT_PROMISE_TO_PAY": "ACTION_SCHEDULED",
    "FOLLOW_UP_PTP": "ACTION_SCHEDULED",
    "ESCALATE": "HUMAN_REVIEW",
    "STOP": "STOPPED",
}

# Baseline policy: for each failure category, the ranked list of actions
# we'd prefer, most restrained first. The guardrail engine gets the final
# say on whether the top choice is actually allowed right now.
CATEGORY_ACTION_PREFERENCE = {
    "TRANSIENT_INFRASTRUCTURE": ["WAIT"],
    "SUBSCRIPTION_PENDING_NATIVE_RETRY": ["WAIT_FOR_NATIVE_RETRY"],
    "INSUFFICIENT_BALANCE": ["WAIT", "COLLECT_PROMISE_TO_PAY", "CONTACT_CUSTOMER", "ESCALATE"],
    "CUSTOMER_AUTHENTICATION": ["CONTACT_CUSTOMER", "ESCALATE"],
    "INVALID_INSTRUMENT": ["CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "ESCALATE"],
    "MANDATE_ISSUE": ["CONTACT_CUSTOMER", "ESCALATE"],
    "PERMANENT_HARD_FAILURE": ["ESCALATE"],
    "UNKNOWN": ["ESCALATE"],  # insufficient confidence -> conservative, human review
}


@dataclass(frozen=True)
class GuardrailConfig:
    """Defaults mirror the example merchant policy in ARCHITECTURE.md."""
    max_contacts_per_case: int = 3
    max_contacts_per_7_days: int = 2
    min_contact_interval_hours: int = 12
    max_automated_amount: Decimal = Decimal("25000")
    max_total_attempts: int = 5  # stopping rule: give up rather than spend forever on one case


DEFAULT_GUARDRAILS = GuardrailConfig()


def _contacts_for_case(db: Session, case: RevenueCase) -> list[Action]:
    return (
        db.query(Action)
        .filter(
            Action.revenue_case_id == case.id,
            Action.action_type.in_(CONTACT_ACTIONS),
            Action.status.in_(("SCHEDULED", "EXECUTED")),
        )
        .all()
    )


def _attempt_count(db: Session, case: RevenueCase) -> int:
    """How many times this case has been (re-)diagnosed — i.e. how many
    separate failure events it has absorbed. Used for the stopping rule:
    past some point, continuing to spend recovery effort on a case that
    keeps failing is worse than giving up."""
    return (
        db.query(AuditEvent)
        .filter(
            AuditEvent.revenue_case_id == case.id,
            AuditEvent.event.in_(("case_detected", "failure_event_received")),
        )
        .count()
    )


def _check_action_allowed(
    db: Session, case: RevenueCase, action: str, config: GuardrailConfig, now: datetime
) -> tuple[bool, str | None]:
    """Returns (allowed, reason_if_blocked)."""
    if action in WAIT_ACTIONS:
        completed_wait = (
            db.query(Action.id)
            .filter(
                Action.revenue_case_id == case.id,
                Action.action_type == action,
                Action.status == "EXECUTED",
            )
            .first()
        )
        if completed_wait:
            return False, f"{action} already completed for this case"

    if action in CONTACT_ACTIONS and case.amount is not None and case.amount > config.max_automated_amount:
        return False, f"amount {case.amount} exceeds max_automated_amount {config.max_automated_amount} — requires human approval"

    if action in CONTACT_ACTIONS:
        contacts = _contacts_for_case(db, case)

        if len(contacts) >= config.max_contacts_per_case:
            return False, f"max_contacts_per_case ({config.max_contacts_per_case}) reached"

        recent = [c for c in contacts if c.created_at and c.created_at >= now - timedelta(days=7)]
        if len(recent) >= config.max_contacts_per_7_days:
            return False, f"max_contacts_per_7_days ({config.max_contacts_per_7_days}) reached"

        if contacts:
            last_contact_at = max(c.created_at for c in contacts if c.created_at)
            cooldown_until = last_contact_at + timedelta(hours=config.min_contact_interval_hours)
            if now < cooldown_until:
                return False, f"cooldown active — next contact allowed after {cooldown_until.isoformat()}"

    return True, None


def _record_decision(
    db: Session, case: RevenueCase, chosen_action: str, alternatives: dict,
    explanation: str, config: GuardrailConfig, now: datetime,
    expected_value: float | None = None,
) -> Decision:
    decision = Decision(
        revenue_case_id=case.id,
        chosen_action=chosen_action,
        expected_value=expected_value,
        alternatives=alternatives,
        guardrails_applied={
            "max_contacts_per_case": config.max_contacts_per_case,
            "max_contacts_per_7_days": config.max_contacts_per_7_days,
            "min_contact_interval_hours": config.min_contact_interval_hours,
            "max_automated_amount": str(config.max_automated_amount),
            "max_total_attempts": config.max_total_attempts,
        },
        explanation=explanation,
    )
    db.add(decision)
    db.flush()

    scheduled_for = now
    if chosen_action == "WAIT":
        scheduled_for = now + timedelta(seconds=settings.WAIT_DELAY_SECONDS)
    elif chosen_action == "WAIT_FOR_NATIVE_RETRY":
        scheduled_for = now + timedelta(seconds=settings.NATIVE_RETRY_DELAY_SECONDS)

    is_terminal_instruction = chosen_action in {"ESCALATE", "STOP"}
    db.add(Action(
        revenue_case_id=case.id,
        decision_id=decision.id,
        action_type=chosen_action,
        status="EXECUTED" if is_terminal_instruction else "SCHEDULED",
        scheduled_for=scheduled_for,
        executed_at=now if is_terminal_instruction else None,
        max_attempts=settings.ACTION_MAX_ATTEMPTS,
    ))
    db.flush()

    case.state = ACTION_TO_STATE[chosen_action]
    case.updated_at = now

    db.add(AuditEvent(
        revenue_case_id=case.id,
        event="decision_made",
        detail={"chosen_action": chosen_action, "resulting_state": case.state, "explanation": explanation},
    ))

    return decision


def decide(db: Session, case: RevenueCase, config: GuardrailConfig = DEFAULT_GUARDRAILS, now: datetime | None = None) -> Decision | None:
    """
    Runs the guardrail-filtered baseline policy for a case that has just
    been diagnosed. No-ops (returns None) if the case isn't in a state this
    engine is allowed to decide for — DECISION_READY is a launching point,
    not something actions get chosen for repeatedly out of nowhere.
    """
    if case.state != "DIAGNOSED":
        return None

    now = now or utc_now()
    case.state = "DECISION_READY"

    if _attempt_count(db, case) > config.max_total_attempts:
        return _record_decision(
            db, case, chosen_action="STOP",
            alternatives={"STOP": {"allowed": True, "reason": "max_total_attempts stopping rule triggered"}},
            explanation=(
                f"Case classified as {case.failure_category}. Stopped: exceeded "
                f"max_total_attempts ({config.max_total_attempts}) — further recovery "
                f"effort is not worth continuing to spend on this case."
            ),
            config=config, now=now,
        )

    candidates = CATEGORY_ACTION_PREFERENCE.get(case.failure_category, ["ESCALATE"])

    alternatives = {}
    chosen_action = None
    chosen_was_top_ranked = False

    for i, action in enumerate(candidates):
        allowed, block_reason = _check_action_allowed(db, case, action, config, now)
        alternatives[action] = {"allowed": allowed, "reason": block_reason}
        if allowed and chosen_action is None:
            chosen_action = action
            chosen_was_top_ranked = (i == 0)

    if chosen_action is None:
        # Every ranked candidate was blocked by a guardrail — fall back to
        # human review rather than doing nothing silently.
        chosen_action = "ESCALATE"
        alternatives.setdefault(
            "ESCALATE", {"allowed": True, "reason": "fallback — all ranked candidates were blocked"}
        )
        chosen_was_top_ranked = False

    explanation = (
        f"Case classified as {case.failure_category}. Chose {chosen_action} "
        f"({'top-ranked candidate for this category' if chosen_was_top_ranked else 'fallback — a higher-ranked option was blocked by a guardrail'})."
    )

    return _record_decision(db, case, chosen_action, alternatives, explanation, config, now)


# ---------------------------------------------------------------------------
# Public API for reuse by other policy engines (Phase 5's ml_policy.py).
# These wrap the private helpers above rather than duplicating their logic,
# so both the baseline and the ML policy check guardrails and record
# decisions identically — the only thing that should differ between them is
# *which action gets chosen*, never how a decision gets recorded or how a
# guardrail gets enforced.
# ---------------------------------------------------------------------------

ALL_ACTIONS = [
    "WAIT", "WAIT_FOR_NATIVE_RETRY", "CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER",
    "COLLECT_PROMISE_TO_PAY", "ESCALATE", "STOP",
]


def check_action_allowed(
    db: Session, case: RevenueCase, action: str,
    config: GuardrailConfig = DEFAULT_GUARDRAILS, now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Public entry point for _check_action_allowed. Returns (allowed, reason_if_blocked)."""
    return _check_action_allowed(db, case, action, config, now or utc_now())


def attempt_count(db: Session, case: RevenueCase) -> int:
    """Public entry point for _attempt_count."""
    return _attempt_count(db, case)


def contacts_for_case(db: Session, case: RevenueCase) -> list[Action]:
    """Public entry point for _contacts_for_case."""
    return _contacts_for_case(db, case)


def record_decision(
    db: Session, case: RevenueCase, chosen_action: str, alternatives: dict,
    explanation: str, config: GuardrailConfig = DEFAULT_GUARDRAILS, now: datetime | None = None,
    expected_value: float | None = None,
) -> Decision:
    """Public entry point for _record_decision."""
    return _record_decision(
        db, case, chosen_action, alternatives, explanation, config,
        now or utc_now(), expected_value=expected_value,
    )
