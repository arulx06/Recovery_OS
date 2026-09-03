"""Promise-to-Pay follow-up state changes for linked temporal actions."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RevenueCase, Action, AuditEvent, PromiseToPay
from app.core.time import utc_now

TERMINAL_STATES = {"RECOVERED", "STOPPED", "DISPUTED"}


def _log_audit(db: Session, case: RevenueCase, event: str, detail: dict | None = None):
    db.add(AuditEvent(revenue_case_id=case.id, event=event, detail=detail or {}))


def complete_followup(db: Session, case: RevenueCase, action: Action, now: datetime) -> str:
    """Complete one claimed or directly-invoked follow-up against its exact promise."""
    promise = None
    if action.promise_to_pay_id:
        promise = (
            db.query(PromiseToPay)
            .filter(PromiseToPay.id == action.promise_to_pay_id)
            .first()
        )

    # Historical rows created before exact linkage was added can be repaired
    # safely only when there is a single unambiguous pending promise.
    if not promise and not action.promise_to_pay_id:
        candidates = (
            db.query(PromiseToPay)
            .filter(PromiseToPay.revenue_case_id == case.id, PromiseToPay.status == "PENDING")
            .limit(2)
            .all()
        )
        if len(candidates) == 1:
            promise = candidates[0]
            action.promise_to_pay_id = promise.id

    if case.state in TERMINAL_STATES or not promise or promise.status != "PENDING":
        if promise and case.state == "RECOVERED" and promise.status == "PENDING":
            promise.status = "KEPT"
        action.status = "EXECUTED"
        action.executed_at = now
        _log_audit(
            db,
            case,
            "ptp_followup_stale",
            {"action_id": action.id, "promise_id": promise.id if promise else None},
        )
        return "already_recovered"

    promise.status = "BROKEN"
    action.status = "EXECUTED"
    action.executed_at = now
    case.state = "HUMAN_REVIEW"
    case.updated_at = now
    _log_audit(db, case, "promise_broken_escalated", {"promise_id": promise.id})
    return "broken"


def process_due_followups(db: Session, now: datetime | None = None) -> dict:
    """
    Processes every SCHEDULED FOLLOW_UP_PTP action whose scheduled_for has
    passed. Returns a summary dict: {"processed": n, "already_recovered": n,
    "broken": n}.
    """
    now = now or utc_now()

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
        outcome = complete_followup(db, case, action, now)
        if outcome == "already_recovered":
            summary["already_recovered"] += 1
        else:
            summary["broken"] += 1

    db.flush()
    return summary
