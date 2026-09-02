"""
Promise-to-Pay follow-up processing (Phase 6).

When a customer promises to pay by a date, RecoveryOS schedules a
FOLLOW_UP_PTP action for that date (see orchestrator.handle_customer_reply).
There's no live background scheduler in this project yet — Redis+RQ are
listed as a dependency for exactly this kind of delayed/scheduled work,
but wiring up a persistent worker process is out of scope for a 14-day
build. This module is the manual/cron-invoked equivalent: call
process_due_followups() (or run scripts/process_followups.py) periodically
and it does the same thing a worker would have done when the due time
arrived.

For each FOLLOW_UP_PTP action whose scheduled_for has passed:
  - if the case already recovered by some other path (a real
    payment.captured/payment_link.paid, or a fresh promise superseded
    this one) — nothing to do, mark the action EXECUTED and move on.
  - if the promise is still PENDING and unpaid, mark it BROKEN and
    escalate the case to HUMAN_REVIEW — matches ARCHITECTURE.md's
    "escalate after broken promises" guardrail intent. RecoveryOS doesn't
    keep silently re-asking; a broken promise is a signal that automation
    should step back.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RevenueCase, Action, AuditEvent, PromiseToPay

TERMINAL_STATES = {"RECOVERED", "STOPPED", "DISPUTED"}


def _log_audit(db: Session, case: RevenueCase, event: str, detail: dict | None = None):
    db.add(AuditEvent(revenue_case_id=case.id, event=event, detail=detail or {}))


def process_due_followups(db: Session, now: datetime | None = None) -> dict:
    """
    Processes every SCHEDULED FOLLOW_UP_PTP action whose scheduled_for has
    passed. Returns a summary dict: {"processed": n, "already_recovered": n,
    "broken": n}.
    """
    now = now or datetime.utcnow()

    due_actions = (
        db.query(Action)
        .filter(Action.action_type == "FOLLOW_UP_PTP", Action.status == "SCHEDULED", Action.scheduled_for <= now)
        .all()
    )

    summary = {"processed": 0, "already_recovered": 0, "broken": 0}

    for action in due_actions:
        case = db.query(RevenueCase).filter(RevenueCase.id == action.revenue_case_id).first()
        if not case:
            continue

        summary["processed"] += 1
        action.status = "EXECUTED"
        action.executed_at = now

        if case.state in TERMINAL_STATES:
            summary["already_recovered"] += 1
            continue

        promise = (
            db.query(PromiseToPay)
            .filter(PromiseToPay.revenue_case_id == case.id, PromiseToPay.status == "PENDING")
            .order_by(PromiseToPay.created_at.desc())
            .first()
        )

        if promise:
            promise.status = "BROKEN"

        case.state = "HUMAN_REVIEW"
        case.updated_at = now
        _log_audit(
            db, case, "promise_broken_escalated",
            {"promise_id": promise.id if promise else None},
        )
        summary["broken"] += 1

    db.flush()
    return summary
