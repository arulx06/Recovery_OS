"""
Action Executor.

Phase 3 built the guardrail-checked policy engine that decides what to do
and records it as a SCHEDULED Action. This module is where a decision
becomes a real-world side effect.

Phase 4 added CREATE_PAYMENT_LINK. Phase 6 adds CONTACT_CUSTOMER and
COLLECT_PROMISE_TO_PAY — both draft an outbound message via the LLM
client (real or simulated) and store it as a CustomerMessage, rather than
leaving the case sitting at ACTION_SCHEDULED indefinitely. WAIT /
WAIT_FOR_NATIVE_RETRY / ESCALATE / STOP still have no external side effect
to execute; the state they leave the case in already reflects the action.

State machine step this owns:

    ACTION_SCHEDULED -> ACTION_EXECUTED -> AWAITING_OUTCOME   (success)
    ACTION_SCHEDULED -> HUMAN_REVIEW                          (execution failed)

A failed API call does not silently retry and does not leave the case
stuck in ACTION_SCHEDULED forever — it falls back to human review, the
same conservative default the policy engine uses whenever it can't decide
confidently on its own.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RevenueCase, Action, AuditEvent, CustomerMessage
from app.services import razorpay_client, llm_client

EXECUTABLE_ACTION_TYPES = {"CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY"}


def _log_audit(db: Session, case: RevenueCase, event: str, detail: dict | None = None):
    db.add(AuditEvent(revenue_case_id=case.id, event=event, detail=detail or {}))


def execute(db: Session, case: RevenueCase, action: Action) -> Action:
    """
    Execute a just-created SCHEDULED action for `case`, if this executor
    knows how to. No-ops (returns the action unchanged) for action types
    outside EXECUTABLE_ACTION_TYPES, or if the action isn't SCHEDULED.
    """
    if action.status != "SCHEDULED" or action.action_type not in EXECUTABLE_ACTION_TYPES:
        return action

    if action.action_type == "CREATE_PAYMENT_LINK":
        return _execute_create_payment_link(db, case, action)

    if action.action_type in ("CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY"):
        return _execute_contact(db, case, action)

    return action  # pragma: no cover — unreachable while EXECUTABLE_ACTION_TYPES is exhaustive above


def _execute_create_payment_link(db: Session, case: RevenueCase, action: Action) -> Action:
    now = datetime.utcnow()

    try:
        result = razorpay_client.create_payment_link(
            amount_rupees=case.amount,
            currency=case.currency or "INR",
            description=f"Recovery for case {case.id} ({case.failure_category or 'unclassified'})",
            reference_id=case.id,
        )
    except razorpay_client.RazorpayAPIError as exc:
        action.status = "FAILED"
        action.executed_at = now
        action.result = {"error": str(exc)}
        case.state = "HUMAN_REVIEW"
        case.updated_at = now
        _log_audit(
            db, case, "action_execution_failed",
            {"action_type": action.action_type, "error": str(exc)},
        )
        db.flush()
        return action

    action.status = "EXECUTED"
    action.executed_at = now
    action.result = result

    case.razorpay_payment_link_id = result.get("id")
    case.state = "AWAITING_OUTCOME"
    case.updated_at = now

    _log_audit(
        db, case, "action_executed",
        {
            "action_type": action.action_type,
            "razorpay_payment_link_id": result.get("id"),
            "short_url": result.get("short_url"),
            "simulated": result.get("simulated", False),
        },
    )
    db.flush()
    return action


def _execute_contact(db: Session, case: RevenueCase, action: Action) -> Action:
    now = datetime.utcnow()

    try:
        draft = llm_client.draft_contact_message(
            action.action_type,
            {
                "amount": float(case.amount) if case.amount is not None else 0.0,
                "failure_category": case.failure_category,
                "case_id": case.id,
            },
        )
    except llm_client.LLMAPIError as exc:
        action.status = "FAILED"
        action.executed_at = now
        action.result = {"error": str(exc)}
        case.state = "HUMAN_REVIEW"
        case.updated_at = now
        _log_audit(
            db, case, "action_execution_failed",
            {"action_type": action.action_type, "error": str(exc)},
        )
        db.flush()
        return action

    db.add(CustomerMessage(
        revenue_case_id=case.id,
        direction="outbound",
        channel="simulated" if draft.get("simulated") else "llm",
        body=draft["body"],
    ))

    action.status = "EXECUTED"
    action.executed_at = now
    action.result = draft

    case.state = "AWAITING_OUTCOME"
    case.updated_at = now

    _log_audit(
        db, case, "action_executed",
        {
            "action_type": action.action_type,
            "message_preview": draft["body"][:120],
            "simulated": draft.get("simulated", False),
        },
    )
    db.flush()
    return action
