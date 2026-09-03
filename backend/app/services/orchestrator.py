"""
Recovery Case Orchestrator.

Owns inbound event and customer-reply mutations of RevenueCase.state.
Policy and claimed-worker services own their narrow transitions; routers,
queue transport, and ML scoring never mutate state directly.

State machine (see ARCHITECTURE.md for the full diagram):

    DETECTED -> DIAGNOSED -> DECISION_READY -> WAITING / ACTION_SCHEDULED / HUMAN_REVIEW
                                              -> DISPUTED
                           -> RECOVERED / STOPPED (from anywhere pre-terminal)

Phase 1 covered DETECTED and the two edge cases the brief calls out
explicitly (duplicate webhook delivery, and payment.failed followed later
by payment.captured for the same payment). Phase 2 added DIAGNOSED: every
failure gets a deterministic failure_category. Phase 3 adds the rest —
right after diagnosis, the Guardrail + baseline Policy Engine
(app/services/policy_engine.py) runs synchronously, choosing an action and
landing the case in WAITING, ACTION_SCHEDULED, or HUMAN_REVIEW.

The baseline policy runs inline. Payment Links may be created through the
explicitly enabled Razorpay Test Mode integration; contact text is drafted
and stored but no messaging transport delivers it.
"""
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import RevenueCase, AuditEvent, PaymentEvent, Action, PromiseToPay, CustomerMessage
from app.core.config import settings
from app.core.time import end_of_local_day_utc, utc_now, utc_to_local
from app.services.failure_diagnosis import classify_failure
from app.services import llm_client, policy_dispatcher, policy_engine, ptp_extractor

FAILURE_EVENTS = {"payment.failed", "subscription.halted"}
# payment.captured / subscription.charged carry the original failed
# payment's own id and match on RevenueCase.razorpay_payment_id.
# payment_link.paid is handled separately below — it fulfills through a
# *different* payment than the one that originally failed, and only the
# payment_link id (RevenueCase.razorpay_payment_link_id) ties it back.
RECOVERY_EVENTS = {"payment.captured", "subscription.charged"}
PAYMENT_LINK_PAID_EVENT = "payment_link.paid"
DISPUTE_EVENTS = {"payment.dispute.created"}

# States a case can be (re-)diagnosed from. A case that's already recovered,
# stopped, or disputed should not be re-diagnosed — diagnosis only matters
# while we're still deciding what to do.
DIAGNOSABLE_STATES = {
    "DETECTED", "DIAGNOSED", "DECISION_READY", "WAITING",
    "ACTION_SCHEDULED", "AWAITING_OUTCOME",
}


def _entity_from_payload(payload: dict, entity_name: str) -> dict:
    """Extract the event-specific Razorpay entity instead of relying on payload order."""
    return (payload or {}).get("payload", {}).get(entity_name, {}).get("entity", {}) or {}


def _payment_link_entity_from_payload(payload: dict) -> dict:
    """
    payment_link.paid carries both a payment_link.entity (the link itself
    — what we match RevenueCase.razorpay_payment_link_id against) and a
    payment.entity (the payment that actually fulfilled it). We only need
    the former to find the case.
    """
    return (payload or {}).get("payload", {}).get("payment_link", {}).get("entity", {}) or {}


def _amount_in_rupees(entity: dict) -> Decimal | None:
    amount = entity.get("amount")
    if amount is None:
        return None
    return Decimal(amount) / Decimal(100)


def _log_audit(db: Session, case: RevenueCase, event: str, detail: dict | None = None):
    db.add(AuditEvent(revenue_case_id=case.id, event=event, detail=detail or {}))


def _matching_case(db: Session, identifiers: list) -> RevenueCase | None:
    if not identifiers:
        return None
    matches = (
        db.query(RevenueCase)
        .filter(RevenueCase.source == "razorpay", or_(*identifiers))
        .with_for_update()
        .all()
    )
    if len(matches) > 1:
        raise RuntimeError("conflicting Razorpay payment and subscription case identifiers")
    return matches[0] if matches else None


def _cancel_scheduled_actions(db: Session, case: RevenueCase) -> int:
    pending = (
        db.query(Action)
        .filter(Action.revenue_case_id == case.id, Action.status == "SCHEDULED")
        .all()
    )
    for action in pending:
        action.status = "CANCELLED"
    return len(pending)


def _prior_unlinked_recovery(
    db: Session,
    razorpay_payment_id: str | None,
    razorpay_subscription_id: str | None,
) -> PaymentEvent | None:
    # A recovery signal must belong to the same receivable/payment attempt
    # before it can suppress automation. Subscription id alone identifies
    # the billing relationship, not a specific cycle, so out-of-order
    # suppression requires an exact payment-id match.
    if not razorpay_payment_id:
        return None
    return (
        db.query(PaymentEvent)
        .filter(
            PaymentEvent.revenue_case_id.is_(None),
            PaymentEvent.event_type.in_(RECOVERY_EVENTS),
            PaymentEvent.razorpay_payment_id == razorpay_payment_id,
        )
        .order_by(PaymentEvent.received_at.desc())
        .with_for_update()
        .first()
    )


def _handle_existing_failure(
    db: Session,
    case: RevenueCase,
    payment_event: PaymentEvent,
    entity: dict,
) -> RevenueCase:
    payment_event.revenue_case_id = case.id
    _log_audit(db, case, "failure_event_received", {"event_id": payment_event.razorpay_event_id})
    if case.state not in DIAGNOSABLE_STATES:
        _log_audit(db, case, "failure_event_ignored_terminal_state", {"state": case.state})
        return case

    pending_promises = (
        db.query(PromiseToPay)
        .filter(PromiseToPay.revenue_case_id == case.id, PromiseToPay.status == "PENDING")
        .all()
    )
    if pending_promises:
        case.error_source = entity.get("error_source") or case.error_source
        case.error_step = entity.get("error_step") or case.error_step
        case.error_reason = entity.get("error_reason") or case.error_reason
        case.failure_category = classify_failure(entity)
        case.updated_at = utc_now()
        _log_audit(
            db,
            case,
            "failure_diagnosed_pending_promise",
            {"pending_promise_ids": [promise.id for promise in pending_promises]},
        )
        return case

    _cancel_scheduled_actions(db, case)
    case.state = "DETECTED"
    _diagnose(db, case, entity)
    return case


def _diagnose(db: Session, case: RevenueCase, entity: dict):
    """Deterministically classify the failure and move DETECTED -> DIAGNOSED."""
    if case.state not in DIAGNOSABLE_STATES:
        return

    previous_category = case.failure_category
    category = classify_failure(entity)

    case.error_source = entity.get("error_source") or case.error_source
    case.error_step = entity.get("error_step") or case.error_step
    case.error_reason = entity.get("error_reason") or case.error_reason
    case.failure_category = category
    case.state = "DIAGNOSED"
    case.updated_at = utc_now()

    if category != previous_category:
        _log_audit(
            db, case, "case_diagnosed",
            {"failure_category": category, "previous_category": previous_category},
        )

    policy_dispatcher.decide_for_case(db, case)


def handle_event(db: Session, payment_event: PaymentEvent, event_type: str, payload: dict) -> RevenueCase | None:
    """
    Given a persisted PaymentEvent, decide what it means for a RevenueCase.
    Returns the affected case, or None if this event type isn't handled yet.
    """
    if event_type == PAYMENT_LINK_PAID_EVENT:
        return _handle_payment_link_paid(db, payment_event, payload)

    entity_name = {
        "payment.failed": "payment",
        "payment.captured": "payment",
        "subscription.halted": "subscription",
        "subscription.charged": "payment",
        "payment.dispute.created": "dispute",
    }.get(event_type)
    entity = _entity_from_payload(payload, entity_name) if entity_name else {}
    if event_type in DISPUTE_EVENTS and not entity:
        entity = _entity_from_payload(payload, "payment")
    razorpay_payment_id = entity.get("id") if entity.get("entity") == "payment" else entity.get("payment_id")
    razorpay_subscription_id = (
        entity.get("id") if entity.get("entity") == "subscription" else entity.get("subscription_id")
    )
    payment_event.razorpay_payment_id = razorpay_payment_id
    payment_event.razorpay_subscription_id = razorpay_subscription_id

    if event_type in FAILURE_EVENTS:
        return _handle_failure(db, payment_event, entity, razorpay_payment_id, razorpay_subscription_id)

    if event_type in RECOVERY_EVENTS:
        return _handle_recovery(db, payment_event, entity, razorpay_payment_id, razorpay_subscription_id)

    if event_type in DISPUTE_EVENTS:
        return _handle_dispute(db, payment_event, entity, razorpay_payment_id)

    return None


def _handle_failure(
    db: Session,
    payment_event: PaymentEvent,
    entity: dict,
    razorpay_payment_id: str | None,
    razorpay_subscription_id: str | None,
) -> RevenueCase:
    existing = None
    identifiers = []
    if razorpay_payment_id:
        identifiers.append(RevenueCase.razorpay_payment_id == razorpay_payment_id)
        existing = _matching_case(db, identifiers)
    if not existing and razorpay_subscription_id:
        existing = (
            db.query(RevenueCase)
            .filter(
                RevenueCase.source == "razorpay",
                RevenueCase.razorpay_subscription_id == razorpay_subscription_id,
                RevenueCase.state.notin_(("RECOVERED", "STOPPED", "DISPUTED", "HUMAN_REVIEW")),
            )
            .order_by(RevenueCase.created_at.desc())
            .with_for_update()
            .first()
        )

    if existing:
        if razorpay_payment_id and existing.razorpay_payment_id != razorpay_payment_id:
            existing.razorpay_payment_id = razorpay_payment_id
            amount = _amount_in_rupees(entity)
            if amount is not None:
                existing.amount = amount
            existing.currency = entity.get("currency") or existing.currency
        return _handle_existing_failure(db, existing, payment_event, entity)

    case = RevenueCase(
        source="razorpay",
        razorpay_payment_id=razorpay_payment_id,
        razorpay_subscription_id=razorpay_subscription_id,
        amount=_amount_in_rupees(entity) or Decimal("0"),
        currency=entity.get("currency", "INR"),
        state="DETECTED",
    )
    try:
        with db.begin_nested():
            db.add(case)
            db.flush()
    except IntegrityError:
        existing = _matching_case(db, identifiers)
        if not existing:
            raise
        return _handle_existing_failure(db, existing, payment_event, entity)

    payment_event.revenue_case_id = case.id
    _log_audit(db, case, "case_detected", {"razorpay_payment_id": razorpay_payment_id})
    prior_recovery = _prior_unlinked_recovery(db, razorpay_payment_id, razorpay_subscription_id)
    if prior_recovery:
        prior_recovery.revenue_case_id = case.id
        return _recover_case(
            db,
            case,
            reason="out_of_order_recovery_event",
            extra_detail={"recovery_event_id": prior_recovery.razorpay_event_id},
        )
    _diagnose(db, case, entity)
    return case


def _recover_case(db: Session, case: RevenueCase, reason: str, extra_detail: dict | None = None) -> RevenueCase:
    """Shared terminal-recovery logic: cancel anything pending, mark RECOVERED."""
    cancelled_count = _cancel_scheduled_actions(db, case)

    kept_promises = (
        db.query(PromiseToPay)
        .filter(PromiseToPay.revenue_case_id == case.id, PromiseToPay.status == "PENDING")
        .all()
    )
    for promise in kept_promises:
        promise.status = "KEPT"

    case.state = "RECOVERED"
    case.updated_at = utc_now()
    detail = {
        "reason": reason,
        "cancelled_actions": cancelled_count,
        "kept_promises": len(kept_promises),
    }
    if extra_detail:
        detail.update(extra_detail)
    _log_audit(db, case, "case_recovered_silently", detail)
    return case


def _handle_recovery(
    db: Session,
    payment_event: PaymentEvent,
    entity: dict,
    razorpay_payment_id: str | None,
    razorpay_subscription_id: str | None,
) -> RevenueCase | None:
    if not razorpay_payment_id and not razorpay_subscription_id:
        return None

    case = None
    if razorpay_payment_id:
        case = _matching_case(db, [RevenueCase.razorpay_payment_id == razorpay_payment_id])
        if not case:
            linked_case_row = (
                db.query(PaymentEvent.revenue_case_id)
                .filter(
                    PaymentEvent.razorpay_payment_id == razorpay_payment_id,
                    PaymentEvent.revenue_case_id.is_not(None),
                )
                .order_by(PaymentEvent.received_at.desc())
                .first()
            )
            linked_case_id = linked_case_row[0] if linked_case_row else None
            if linked_case_id:
                case = (
                    db.query(RevenueCase)
                    .filter(RevenueCase.id == linked_case_id, RevenueCase.source == "razorpay")
                    .with_for_update()
                    .first()
                )
    if not case and razorpay_subscription_id:
        case = (
            db.query(RevenueCase)
            .filter(
                RevenueCase.source == "razorpay",
                RevenueCase.razorpay_subscription_id == razorpay_subscription_id,
                RevenueCase.state.notin_(("RECOVERED", "DISPUTED")),
            )
            .order_by(RevenueCase.created_at.desc())
            .with_for_update()
            .first()
        )
    if not case:
        # Recovery event with no matching failed case — nothing to do yet
        # (e.g. a payment that succeeded on the first try never opened a case).
        return None

    payment_event.revenue_case_id = case.id

    if case.state in ("RECOVERED", "DISPUTED"):
        _log_audit(db, case, "recovery_event_ignored_terminal_state", {"state": case.state})
        return case

    # This is the documented Razorpay edge case: payment.failed followed
    # later by payment.captured for the same transaction.
    return _recover_case(db, case, reason="late_capture_or_customer_retry")


def _handle_payment_link_paid(db: Session, payment_event: PaymentEvent, payload: dict) -> RevenueCase | None:
    """
    A RecoveryOS-created Payment Link (Phase 4) was paid. This fulfills
    through a *new* payment, distinct from the one that originally failed
    — so matching goes through razorpay_payment_link_id, not
    razorpay_payment_id.
    """
    link_entity = _payment_link_entity_from_payload(payload)
    link_id = link_entity.get("id")
    payment_event.razorpay_payment_link_id = link_id
    payment_entity = _entity_from_payload(payload, "payment")
    payment_event.razorpay_payment_id = payment_entity.get("id")
    if not link_id:
        return None

    case = (
        db.query(RevenueCase)
        .filter(
            RevenueCase.source == "razorpay",
            RevenueCase.razorpay_payment_link_id == link_id,
        )
        .with_for_update()
        .first()
    )
    if not case:
        payment_event.revenue_case_id = None
        return None

    payment_event.revenue_case_id = case.id

    if case.state in ("RECOVERED", "DISPUTED"):
        _log_audit(db, case, "recovery_event_ignored_terminal_state", {"state": case.state})
        return case

    return _recover_case(
        db, case, reason="payment_link_paid",
        extra_detail={"razorpay_payment_link_id": link_id},
    )


def _dispute_case(db: Session, case: RevenueCase, reason: str, extra_detail: dict | None = None) -> RevenueCase:
    """
    Shared terminal-dispute logic: cancel anything pending, mark DISPUTED.
    Deliberately no "terminal state" early-return before this runs — unlike
    recovery, a dispute always wins, even over an already-RECOVERED case
    (a chargeback can land after the fact).
    """
    cancelled = (
        db.query(Action)
        .filter(Action.revenue_case_id == case.id, Action.status == "SCHEDULED")
        .all()
    )
    for action in cancelled:
        action.status = "CANCELLED"

    previous_state = case.state
    case.state = "DISPUTED"
    case.updated_at = utc_now()
    detail = {"reason": reason, "previous_state": previous_state, "cancelled_actions": len(cancelled)}
    if extra_detail:
        detail.update(extra_detail)
    _log_audit(db, case, "case_disputed", detail)
    return case


def _handle_dispute(db: Session, payment_event: PaymentEvent, entity: dict, razorpay_payment_id: str | None) -> RevenueCase | None:
    """A dispute reported directly by Razorpay (payment.dispute.created)."""
    if not razorpay_payment_id:
        return None

    case = (
        db.query(RevenueCase)
        .filter(
            RevenueCase.source == "razorpay",
            RevenueCase.razorpay_payment_id == razorpay_payment_id,
        )
        .with_for_update()
        .first()
    )
    if not case:
        return None

    payment_event.revenue_case_id = case.id
    return _dispute_case(db, case, reason="razorpay_dispute_webhook")


# States a customer reply can meaningfully be processed from — same idea
# as DIAGNOSABLE_STATES, but for inbound messages rather than failure
# events. A reply to an already-terminal case (RECOVERED/STOPPED/DISPUTED)
# is stored for the record but doesn't change anything.
REPLYABLE_STATES = {"DECISION_READY", "WAITING", "ACTION_SCHEDULED", "AWAITING_OUTCOME", "HUMAN_REVIEW"}


def handle_customer_reply(
    db: Session,
    case: RevenueCase,
    message_body: str,
    now: datetime | None = None,
    business_now: datetime | None = None,
    extraction: llm_client.PTPExtraction | None = None,
) -> RevenueCase:
    """
    Phase 6 entry point for an inbound customer message — the reply to a
    CONTACT_CUSTOMER / COLLECT_PROMISE_TO_PAY outreach. Always stores the
    message. Then:

      - "this is wrong" / "already paid" / etc. -> dispute -> case DISPUTED,
        recovery stops immediately (reuses the same _dispute_case path a
        real Razorpay dispute webhook goes through).
      - "I'll pay ₹8,000 Friday" -> validated (ptp_extractor) -> a
        PromiseToPay row + a FOLLOW_UP_PTP action scheduled for that date
        -> case AWAITING_OUTCOME.
      - anything else, or a promise that fails validation (amount too
        high, date out of range, low confidence) -> falls back to
        HUMAN_REVIEW rather than guessing.
    """
    now = now or utc_now()
    business_now = business_now or utc_to_local(now, settings.MERCHANT_TIMEZONE)

    extraction = extraction or llm_client.extract_ptp_intent(message_body, now=business_now)

    # Persist inbound message with provenance — customer text canonical record
    # generation_method / llm metadata stored on CustomerMessage for auditability
    inbound_msg = CustomerMessage(
        revenue_case_id=case.id,
        direction="inbound",
        channel="simulated" if extraction.simulated else "llm",
        body=message_body,
        extracted={
            "intent": extraction.intent,
            "amount": extraction.amount,
            "promised_amount": extraction.promised_amount,
            "date": extraction.date,
            "promised_date": extraction.promised_date,
            "confidence": extraction.confidence,
            "reasoning_code": extraction.reasoning_code,
            "provider": extraction.provider,
            "model": extraction.model,
            "prompt_version": extraction.prompt_version,
            "schema_version": extraction.schema_version,
            "extraction_method": extraction.extraction_method,
        },
        generation_method=extraction.extraction_method or ("deterministic" if extraction.simulated else "llm"),
        llm_provider=extraction.provider,
        llm_model=extraction.model,
        prompt_version=extraction.prompt_version,
        schema_version=extraction.schema_version,
        status="RECEIVED",
    )
    db.add(inbound_msg)
    db.flush()

    # Observability: extraction completed audit (sanitized, no raw customer text)
    _log_audit(
        db, case, "ptp_extraction_completed",
        {
            "intent": extraction.intent,
            "confidence": extraction.confidence,
            "extraction_method": extraction.extraction_method,
            "provider": extraction.provider,
            "model": extraction.model,
            "prompt_version": extraction.prompt_version,
            "reasoning_code": extraction.reasoning_code,
        },
    )

    if case.state not in REPLYABLE_STATES:
        _log_audit(db, case, "customer_reply_ignored_terminal_state", {"state": case.state})
        db.flush()
        return case

    # Payment claim / dispute — never mark RECOVERED, always conservative DISPUTED / manual
    if extraction.intent in ("dispute", "payment_claim"):
        _log_audit(db, case, "customer_payment_claim_received", {"intent": extraction.intent, "extraction_method": extraction.extraction_method})
        _dispute_case(db, case, reason="customer_reported_dispute", extra_detail={"intent": extraction.intent})
        db.flush()
        return case

    # Not-a-promise / do-not-contact
    if extraction.intent in ("not_a_promise",):
        _log_audit(db, case, "ptp_extraction_uncertain", {"reason": "not_a_promise / opt_out", "intent": extraction.intent})
        _cancel_scheduled_actions(db, case)
        case.state = "HUMAN_REVIEW"
        case.updated_at = now
        _log_audit(db, case, "ptp_validation_rejected", {"reason": "not_a_promise", "intent": extraction.intent})
        db.flush()
        return case

    outstanding = float(case.amount) if case.amount is not None else 0.0
    # Case eligibility checks per spec 12
    if case.state in ("RECOVERED", "DISPUTED", "STOPPED"):
        _log_audit(db, case, "ptp_validation_rejected", {"reason": f"case state {case.state} not eligible", "intent": extraction.intent})
        _cancel_scheduled_actions(db, case)
        case.state = "HUMAN_REVIEW"
        case.updated_at = now
        db.flush()
        return case

    validation = ptp_extractor.validate_promise(
        extraction, outstanding_amount=outstanding, now=business_now,
    )

    if not validation.valid:
        # Use deterministic validation result
        _cancel_scheduled_actions(db, case)
        case.state = "HUMAN_REVIEW"
        case.updated_at = now
        _log_audit(
            db, case, "ptp_validation_failed",
            {"reason": validation.reason, "extraction": {"intent": extraction.intent, "amount": extraction.amount, "promised_amount": extraction.promised_amount, "date": extraction.date, "promised_date": extraction.promised_date, "confidence": extraction.confidence, "reasoning_code": extraction.reasoning_code}},
        )
        db.flush()
        return case

    _cancel_scheduled_actions(db, case)
    previous_promises = (
        db.query(PromiseToPay)
        .filter(PromiseToPay.revenue_case_id == case.id, PromiseToPay.status == "PENDING")
        .all()
    )
    previous_ids = [promise.id for promise in previous_promises]
    for previous in previous_promises:
        previous.status = "SUPERSEDED"
    if previous_ids:
        previous_followups = (
            db.query(Action)
            .filter(
                Action.promise_to_pay_id.in_(previous_ids),
                Action.status == "SCHEDULED",
            )
            .all()
        )
        for action in previous_followups:
            action.status = "CANCELLED"

    promise = PromiseToPay(
        revenue_case_id=case.id,
        promised_amount=validation.amount,
        promised_date=datetime.combine(validation.promised_date, time.min),
        confidence=extraction.confidence,
        status="PENDING",
        extraction_method=extraction.extraction_method or ("deterministic" if extraction.simulated else "llm"),
        llm_provider=extraction.provider,
        llm_model=extraction.model,
        prompt_version=extraction.prompt_version,
        schema_version=extraction.schema_version,
        amount_method=validation.amount_method,
        reasoning_code=extraction.reasoning_code or validation.reasoning_code,
        source_message_id=inbound_msg.id,
    )
    db.add(promise)
    db.flush()

    followup_at = end_of_local_day_utc(validation.promised_date, settings.MERCHANT_TIMEZONE)
    db.add(Action(
        revenue_case_id=case.id,
        promise_to_pay_id=promise.id,
        action_type="FOLLOW_UP_PTP",
        status="SCHEDULED",
        scheduled_for=followup_at,
        max_attempts=settings.ACTION_MAX_ATTEMPTS,
    ))

    case.state = "AWAITING_OUTCOME"
    case.updated_at = now
    _log_audit(
        db, case, "promise_to_pay_recorded",
        {
            "promise_id": promise.id,
            "amount": float(promise.promised_amount),
            "promised_date": validation.promised_date.isoformat(),
            "followup_at_utc": followup_at.isoformat(),
            "merchant_timezone": settings.MERCHANT_TIMEZONE,
            "superseded_promises": len(previous_promises),
            "extraction_method": promise.extraction_method,
            "provider": promise.llm_provider,
            "model": promise.llm_model,
            "prompt_version": promise.prompt_version,
            "amount_method": promise.amount_method,
            "confidence": float(promise.confidence) if promise.confidence is not None else None,
            "source_message_id": promise.source_message_id,
        },
    )
    db.flush()
    return case
