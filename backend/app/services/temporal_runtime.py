"""PostgreSQL-authoritative claiming, execution, retry, and reconciliation."""
from datetime import timedelta

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.time import utc_now
from app.models import Action, AuditEvent, PromiseToPay, RevenueCase
from app.services import action_executor, policy_engine, ptp_followup, razorpay_client, task_queue


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


def _reconcile_payment_link(action_id: str, case_snapshot, now) -> dict | None:
    """Attempt provider reconciliation for one CREATE_PAYMENT_LINK.

    Returns sanitized provider link if found and validated, None if reliably
    absent, and raises RazorpayAPIError on mismatch or RazorpayAmbiguousError
    if lookup itself is ambiguous (caller must retry later).
    """
    expected_amount_paise = int(case_snapshot.amount * 100) if case_snapshot.amount else 0
    expected_currency = case_snapshot.currency or "INR"
    expected_case_id = case_snapshot.id
    try:
        link = razorpay_client.find_payment_link_for_action(
            action_id, expected_amount_paise, expected_currency, expected_case_id,
        )
    except razorpay_client.RazorpayAmbiguousError:
        raise
    except razorpay_client.RazorpayAPIError:
        raise
    return link


def _handle_ambiguous_payment_link(action_id: str, now, error_detail: str) -> str:
    # Audit the ambiguity before any provider I/O.
    with SessionLocal() as db:
        action = db.query(Action).filter(Action.id == action_id).first()
        if action:
            db.add(AuditEvent(
                revenue_case_id=action.revenue_case_id,
                event="provider_outcome_ambiguous",
                detail={"action_id": action_id, "error": error_detail[:300]},
            ))
            db.commit()

    snapshot = _execution_snapshot(action_id)
    if not snapshot:
        return "stale"
    _, action_type, case_snapshot = snapshot
    if action_type != "CREATE_PAYMENT_LINK":
        return _record_external_failure(action_id, now)

    # Reload authoritative case state before provider lookup (spec 38).
    with SessionLocal() as db:
        case = db.query(RevenueCase).filter(RevenueCase.id == case_snapshot.id).first()
        if not case or case.state in TERMINAL_CASE_STATES:
            db.rollback()
            # Terminal — cancel stale and do not reconcile.
            with SessionLocal() as cdb:
                action2, case2 = _lock_action_and_case(cdb, action_id)
                if action2 and action2.status == "EXECUTING":
                    _cancel_stale(cdb, action2, now, "case_terminal_before_reconcile")
                    cdb.commit()
                else:
                    cdb.rollback()
            return "stale"
        db.rollback()

    try:
        link = _reconcile_payment_link(action_id, case_snapshot, now)
    except razorpay_client.RazorpayAmbiguousError as exc:
        # Lookup itself ambiguous — keep EXECUTING lease to expire and retry
        # later rather than blindly creating another link. Bounded by attempt_count.
        with SessionLocal() as db:
            action, case = _lock_action_and_case(db, action_id)
            if not action or action.status != "EXECUTING":
                db.rollback()
                return "stale"
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="payment_link_reconciliation_pending",
                detail={"action_id": action_id, "reason": str(exc)[:200]},
            ))
            db.commit()
        # Treat as retry-scheduled: reset to SCHEDULED with delay so next
        # attempt will reconcile again. Do not create a new provider link now.
        return _schedule_ambiguous_retry(action_id, now)
    except razorpay_client.RazorpayAPIError as exc:
        # Mismatch — never adopt, go to HUMAN_REVIEW.
        with SessionLocal() as db:
            action, case = _lock_action_and_case(db, action_id)
            if not action or not case or action.status != "EXECUTING":
                db.rollback()
                return "stale"
            action_executor.apply_reconciliation_failure(db, case, action, str(exc))
            db.commit()
        return "failed"

    if link is not None:
        # Found and validated — adopt via canonical success path with reconciled marker.
        reconciled_result = dict(link)
        reconciled_result["_reconciled"] = True
        reconciled_result["simulated"] = False
        # Ensure reference_id present for audit.
        if "reference_id" not in reconciled_result:
            reconciled_result["reference_id"] = action_id
        with SessionLocal() as db:
            action, case = _lock_action_and_case(db, action_id)
            if not action or not case or action.status != "EXECUTING":
                db.rollback()
                return "stale"
            if case.state != "ACTION_SCHEDULED" or _is_superseded(db, action):
                _cancel_stale(db, action, now, "case_state_or_newer_action")
                db.commit()
                return "stale"
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="payment_link_reconciliation_started",
                detail={"action_id": action_id, "provider_link_id": link.get("id")},
            ))
            db.commit()
        outcome = _complete_external(action_id, reconciled_result, now)
        # Overwrite audit from apply_success to reconciled variant if needed.
        # apply_success already emitted action_reconciled when _reconciled flag set.
        return outcome

    # Reliably absent — safe to retry the create (bounded).
    with SessionLocal() as db:
        action = db.query(Action).filter(Action.id == action_id).first()
        if action:
            db.add(AuditEvent(
                revenue_case_id=action.revenue_case_id,
                event="payment_link_reconciliation_absent",
                detail={"action_id": action_id},
            ))
            db.commit()
    return _record_external_failure(action_id, now)


def _schedule_ambiguous_retry(action_id: str, now) -> str:
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
            action.last_error = "provider_outcome_ambiguous"
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="payment_link_reconciliation_retry_scheduled",
                detail={"action_id": action.id, "attempt": action.attempt_count, "delay_seconds": delay},
            ))
            retry = True
            outcome = "reconcile_retry_scheduled"
        else:
            # Exhausted ambiguous retries — conservative manual review.
            action.status = "FAILED"
            action.executed_at = now
            action.result = {"error": "provider reconciliation unresolved — manual review required"}
            action.last_error = "provider reconciliation unresolved"
            case.state = "HUMAN_REVIEW"
            case.updated_at = now
            db.add(AuditEvent(
                revenue_case_id=case.id,
                event="payment_link_manual_review_required",
                detail={"action_id": action.id, "reason": "reconciliation_attempts_exhausted"},
            ))
            outcome = "failed"
        db.commit()
    if retry:
        task_queue.enqueue_action(action_id)
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


def _reconcile_stale_payment_link(action_id: str, now, stale_before) -> str | None:
    """Provider-aware stale handling for CREATE_PAYMENT_LINK. Returns outcome or None to fall back."""
    snapshot = _execution_snapshot(action_id)
    if not snapshot:
        return None
    _, action_type, case_snapshot = snapshot
    if action_type != "CREATE_PAYMENT_LINK":
        return None

    # Check authoritative case state first.
    with SessionLocal() as db:
        case = db.query(RevenueCase).filter(RevenueCase.id == case_snapshot.id).first()
        action = db.query(Action).filter(Action.id == action_id).first()
        if not action or action.status != "EXECUTING" or action.claimed_at is None or action.claimed_at > stale_before:
            db.rollback()
            return "stale"
        if not case or case.state in TERMINAL_CASE_STATES or _is_superseded(db, action):
            db.rollback()
            with SessionLocal() as cdb:
                a, c = _lock_action_and_case(cdb, action_id)
                if a and a.status == "EXECUTING":
                    _cancel_stale(cdb, a, now, "ineligible_expired_claim")
                    cdb.commit()
                else:
                    cdb.rollback()
            return "stale"
        # Determine if reconciliation is needed — always for payment links with
        # potential provider ambiguity. Do provider lookup outside the DB txn.
        db.rollback()

    with SessionLocal() as db:
        action_check = db.query(Action).filter(Action.id == action_id).first()
        case_check = db.query(RevenueCase).filter(RevenueCase.id == case_snapshot.id).first()
        if not action_check or not case_check:
            db.rollback()
            return "stale"
        db.add(AuditEvent(
            revenue_case_id=case_check.id,
            event="payment_link_reconciliation_started",
            detail={"action_id": action_id, "reason": "stale_executing_claim"},
        ))
        db.commit()

    try:
        link = _reconcile_payment_link(action_id, case_snapshot, now)
    except razorpay_client.RazorpayAmbiguousError as exc:
        # Lookup ambiguous — keep lease to retry later; do not reset to SCHEDULED
        # that would immediately recreate. Bounded by next reconcile cycle.
        with SessionLocal() as db:
            action, case = _lock_action_and_case(db, action_id)
            if action and action.status == "EXECUTING":
                db.add(AuditEvent(
                    revenue_case_id=case.id,
                    event="payment_link_reconciliation_pending",
                    detail={"action_id": action_id, "reason": str(exc)[:200]},
                ))
                db.commit()
        return "reconcile_pending"
    except razorpay_client.RazorpayAPIError as exc:
        with SessionLocal() as db:
            action, case = _lock_action_and_case(db, action_id)
            if action and case and action.status == "EXECUTING":
                action_executor.apply_reconciliation_failure(db, case, action, str(exc))
                db.commit()
        return "failed"

    if link is not None:
        reconciled_result = dict(link)
        reconciled_result["_reconciled"] = True
        reconciled_result["simulated"] = False
        if "reference_id" not in reconciled_result:
            reconciled_result["reference_id"] = action_id
        return _complete_external(action_id, reconciled_result, now)

    # Absent — allow normal stale reset to retry create.
    return None


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
    except razorpay_client.RazorpayAmbiguousError as exc:
        return _handle_ambiguous_payment_link(action_id, now, str(exc))
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
        # Provider-aware path for stale CREATE_PAYMENT_LINK — check provider
        # before blindly resetting to SCHEDULED (spec: 14, 17).
        stale_snapshot = _execution_snapshot(action_id)
        if stale_snapshot and stale_snapshot[1] == "CREATE_PAYMENT_LINK":
            outcome = _reconcile_stale_payment_link(action_id, now, stale_before)
            if outcome is not None:
                if outcome == "failed":
                    failed += 1
                continue
            # absent (None) -> fall through to generic lease recovery
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
