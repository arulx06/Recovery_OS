"""PostgreSQL-authoritative claiming, execution, retry, and reconciliation."""
from datetime import timedelta

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.time import utc_now
from app.models import Action, AuditEvent, PromiseToPay, RevenueCase
from app.services import action_executor, policy_engine, ptp_followup, task_queue


TERMINAL_CASE_STATES = {"RECOVERED", "STOPPED", "DISPUTED"}
WAIT_ACTION_TYPES = {"WAIT", "WAIT_FOR_NATIVE_RETRY"}


def _action_case_id(action_id: str) -> str | None:
    with SessionLocal() as db:
        case_id = db.query(Action.revenue_case_id).filter(Action.id == action_id).scalar()
        db.rollback()
        return case_id


def _claim_action(action_id: str, now) -> bool:
    case_id = _action_case_id(action_id)
    if not case_id:
        return False

    with SessionLocal.begin() as db:
        case = (
            db.query(RevenueCase)
            .filter(RevenueCase.id == case_id)
            .with_for_update()
            .first()
        )
        if not case:
            return False
        action = db.query(Action).filter(Action.id == action_id).with_for_update().first()
        if not action or action.status != "SCHEDULED":
            return False
        if action.scheduled_for and action.scheduled_for > now:
            return False

        eligible = case.state not in TERMINAL_CASE_STATES
        if action.action_type in WAIT_ACTION_TYPES:
            eligible = eligible and case.state == "WAITING" and not _is_superseded(db, action)
        elif action.action_type in action_executor.EXECUTABLE_ACTION_TYPES:
            eligible = eligible and case.state == "ACTION_SCHEDULED" and not _is_superseded(db, action)
        elif action.action_type == "FOLLOW_UP_PTP":
            promise = None
            if action.promise_to_pay_id:
                promise = db.query(PromiseToPay).filter(PromiseToPay.id == action.promise_to_pay_id).first()
            else:
                candidates = (
                    db.query(PromiseToPay)
                    .filter(PromiseToPay.revenue_case_id == case.id, PromiseToPay.status == "PENDING")
                    .limit(2)
                    .all()
                )
                if len(candidates) == 1:
                    promise = candidates[0]
                    action.promise_to_pay_id = promise.id
            eligible = (
                eligible
                and case.state == "AWAITING_OUTCOME"
                and promise is not None
                and promise.status == "PENDING"
                and not _is_superseded(db, action)
            )
        else:
            eligible = False

        if not eligible:
            _cancel_stale(db, action, now, "ineligible_at_claim")
            return False

        action.status = "EXECUTING"
        action.claimed_at = now
        action.attempt_count += 1
        db.add(AuditEvent(
            revenue_case_id=case_id,
            event="action_claimed",
            detail={"action_id": action_id, "attempt": action.attempt_count},
        ))
        return True


def _lock_action_and_case(db, action_id: str):
    case_id = db.query(Action.revenue_case_id).filter(Action.id == action_id).scalar()
    if not case_id:
        return None, None
    db.rollback()
    case = (
        db.query(RevenueCase)
        .filter(RevenueCase.id == case_id)
        .with_for_update()
        .first()
    )
    action = db.query(Action).filter(Action.id == action_id).with_for_update().first()
    return action, case


def _is_superseded(db, action: Action) -> bool:
    return (
        db.query(Action.id)
        .filter(
            Action.revenue_case_id == action.revenue_case_id,
            Action.id != action.id,
            Action.created_at > action.created_at,
            Action.status.in_(("SCHEDULED", "EXECUTING", "EXECUTED")),
        )
        .first()
        is not None
    )


def _claimed_action_is_eligible(db, action: Action, case: RevenueCase) -> bool:
    if case.state in TERMINAL_CASE_STATES or _is_superseded(db, action):
        return False
    if action.action_type in WAIT_ACTION_TYPES:
        return case.state == "WAITING"
    if action.action_type in action_executor.EXECUTABLE_ACTION_TYPES:
        return case.state == "ACTION_SCHEDULED"
    if action.action_type == "FOLLOW_UP_PTP":
        promise = db.query(PromiseToPay).filter(PromiseToPay.id == action.promise_to_pay_id).first()
        return case.state == "AWAITING_OUTCOME" and promise is not None and promise.status == "PENDING"
    return False


def _cancel_stale(db, action: Action, now, reason: str) -> str:
    action.status = "CANCELLED"
    action.executed_at = now
    db.add(AuditEvent(
        revenue_case_id=action.revenue_case_id,
        event="stale_action_cancelled",
        detail={"action_id": action.id, "reason": reason},
    ))
    return "stale"


def _complete_wait(action_id: str, now) -> str:
    case_id = None
    with SessionLocal() as db:
        action, case = _lock_action_and_case(db, action_id)
        if not action or not case or action.status != "EXECUTING":
            db.rollback()
            return "stale"
        case_id = case.id
        if case.state != "WAITING" or _is_superseded(db, action):
            outcome = _cancel_stale(db, action, now, "case_state_or_newer_action")
        else:
            action.status = "EXECUTED"
            action.executed_at = now
            case.state = "DIAGNOSED"
            case.updated_at = now
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="wait_completed",
                detail={"action_id": action.id, "action_type": action.action_type},
            ))
            db.flush()
            policy_engine.decide(db, case, now=now)
            outcome = "executed"
        db.commit()
    if case_id:
        task_queue.enqueue_case_actions(case_id)
    return outcome


def _complete_ptp(action_id: str, now) -> str:
    with SessionLocal() as db:
        action, case = _lock_action_and_case(db, action_id)
        if not action or not case or action.status != "EXECUTING":
            db.rollback()
            return "stale"
        if case.state != "AWAITING_OUTCOME" or _is_superseded(db, action):
            outcome = _cancel_stale(db, action, now, "case_state_or_newer_action")
        else:
            outcome = ptp_followup.complete_followup(db, case, action, now)
        db.commit()
        return outcome


def _execution_snapshot(action_id: str):
    with SessionLocal() as db:
        action = db.query(Action).filter(Action.id == action_id).first()
        if not action:
            return None
        case = db.query(RevenueCase).filter(RevenueCase.id == action.revenue_case_id).first()
        if not case:
            return None
        snapshot = (
            action.id,
            action.action_type,
            action_executor.snapshot_case(case),
        )
        db.rollback()
        return snapshot


def _complete_external(action_id: str, result: dict, now) -> str:
    with SessionLocal() as db:
        action, case = _lock_action_and_case(db, action_id)
        if not action or not case or action.status != "EXECUTING":
            db.rollback()
            return "stale"
        if case.state != "ACTION_SCHEDULED" or _is_superseded(db, action):
            outcome = _cancel_stale(db, action, now, "case_state_or_newer_action")
        else:
            action_executor.apply_success(db, case, action, result)
            outcome = "executed"
        db.commit()
        return outcome


def _record_external_failure(action_id: str, now) -> str:
    retry = False
    with SessionLocal() as db:
        action, case = _lock_action_and_case(db, action_id)
        if not action or not case or action.status != "EXECUTING":
            db.rollback()
            return "stale"
        if case.state != "ACTION_SCHEDULED" or _is_superseded(db, action):
            outcome = _cancel_stale(db, action, now, "case_state_or_newer_action")
        elif action.attempt_count < action.max_attempts:
            delay = settings.ACTION_RETRY_DELAY_SECONDS * (2 ** (action.attempt_count - 1))
            action.status = "SCHEDULED"
            action.scheduled_for = now + timedelta(seconds=delay)
            action.claimed_at = None
            action.enqueued_at = None
            action.queue_job_id = None
            action.last_error = "external service request failed"
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="action_retry_scheduled",
                detail={"action_id": action.id, "attempt": action.attempt_count, "delay_seconds": delay},
            ))
            retry = True
            outcome = "retry_scheduled"
        else:
            action_executor.apply_terminal_failure(db, case, action)
            outcome = "failed"
        db.commit()
    if retry:
        task_queue.enqueue_action(action_id)
    return outcome


def process_action(action_id: str, now=None) -> str:
    """RQ entry point. Duplicate or stale deliveries are safe no-ops."""
    now = now or utc_now()
    if not _claim_action(action_id, now):
        return "stale"

    snapshot = _execution_snapshot(action_id)
    if not snapshot:
        return "stale"
    claimed_id, action_type, case = snapshot

    if action_type in WAIT_ACTION_TYPES:
        return _complete_wait(action_id, now)
    if action_type == "FOLLOW_UP_PTP":
        return _complete_ptp(action_id, now)
    if action_type not in action_executor.EXECUTABLE_ACTION_TYPES:
        return "stale"

    try:
        result = action_executor.perform(claimed_id, action_type, case)
    except action_executor.EXPECTED_EXECUTION_ERRORS:
        return _record_external_failure(action_id, now)
    return _complete_external(action_id, result, now)


def reconcile_actions(now=None, limit: int | None = None) -> dict:
    """Recover expired claims and republish all durable scheduled actions."""
    now = now or utc_now()
    stale_before = now - timedelta(seconds=settings.ACTION_CLAIM_TIMEOUT_SECONDS)
    reset = 0
    failed = 0

    with SessionLocal() as db:
        stale_query = db.query(Action.id).filter(
            Action.status == "EXECUTING", Action.claimed_at <= stale_before,
        )
        if limit is not None:
            stale_query = stale_query.limit(limit)
        stale_ids = [row[0] for row in stale_query.all()]
        db.rollback()

    for action_id in stale_ids:
        with SessionLocal() as db:
            action, case = _lock_action_and_case(db, action_id)
            if not action or not case or action.status != "EXECUTING" or action.claimed_at > stale_before:
                db.rollback()
                continue
            if not _claimed_action_is_eligible(db, action, case):
                _cancel_stale(db, action, now, "ineligible_expired_claim")
            elif action.attempt_count >= action.max_attempts:
                action_executor.apply_terminal_failure(db, case, action)
                failed += 1
            else:
                action.status = "SCHEDULED"
                action.scheduled_for = now
                action.claimed_at = None
                action.enqueued_at = None
                action.queue_job_id = None
                action.last_error = "execution lease expired"
                db.add(AuditEvent(
                    revenue_case_id=case.id,
                    event="action_claim_recovered",
                    detail={"action_id": action.id, "attempt": action.attempt_count},
                ))
                reset += 1
            db.commit()

    with SessionLocal() as db:
        scheduled_query = (
            db.query(Action.id)
            .join(RevenueCase, RevenueCase.id == Action.revenue_case_id)
            .filter(
                Action.status == "SCHEDULED",
                Action.action_type.in_(task_queue.QUEUEABLE_ACTION_TYPES),
                RevenueCase.source == "razorpay",
            )
            .order_by(Action.enqueued_at.asc(), Action.scheduled_for.asc())
        )
        if limit is not None:
            scheduled_query = scheduled_query.limit(limit)
        scheduled_ids = [row[0] for row in scheduled_query.all()]
        db.rollback()

    enqueued = sum(task_queue.enqueue_action(action_id) for action_id in scheduled_ids)
    return {"reset": reset, "failed": failed, "scheduled": len(scheduled_ids), "enqueued": enqueued}
