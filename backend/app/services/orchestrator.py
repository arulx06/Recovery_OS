"""
Recovery Case Orchestrator.

Owns every mutation of RevenueCase.state. Nothing else is allowed to touch
it directly — that invariant is what keeps the state machine honest as
more layers (guardrails, ML policy, LLM) get added on top of it.

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

Nothing is actually executed yet (no message sent, no Payment Link
created) — that's Phase 4. This module and policy_engine.py only decide.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import RevenueCase, AuditEvent, PaymentEvent, Action, PromiseToPay, CustomerMessage
from app.services.failure_diagnosis import classify_failure
from app.services import policy_engine, action_executor, llm_client, ptp_extractor

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
DIAGNOSABLE_STATES = {"DETECTED", "DIAGNOSED"}


def _entity_from_payload(payload: dict) -> dict:
    """Razorpay nests the actual object under payload.<type>.entity."""
    for _, wrapper in (payload or {}).get("payload", {}).items():
        entity = wrapper.get("entity") if isinstance(wrapper, dict) else None
        if entity:
            return entity
    return {}


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
    case.updated_at = datetime.utcnow()

    if category != previous_category:
        _log_audit(
            db, case, "case_diagnosed",
            {"failure_category": category, "previous_category": previous_category},
        )

    decision = policy_engine.decide(db, case)
    if decision:
        action = (
            db.query(Action)
            .filter(Action.decision_id == decision.id)
            .first()
        )
        if action:
            action_executor.execute(db, case, action)


def handle_event(db: Session, payment_event: PaymentEvent, event_type: str, payload: dict) -> RevenueCase | None:
    """
    Given a persisted PaymentEvent, decide what it means for a RevenueCase.
    Returns the affected case, or None if this event type isn't handled yet.
    """
    if event_type == PAYMENT_LINK_PAID_EVENT:
        return _handle_payment_link_paid(db, payment_event, payload)

    entity = _entity_from_payload(payload)
    razorpay_payment_id = entity.get("id") if entity.get("entity") == "payment" else entity.get("payment_id")

    if event_type in FAILURE_EVENTS:
        return _handle_failure(db, payment_event, entity, razorpay_payment_id)

    if event_type in RECOVERY_EVENTS:
        return _handle_recovery(db, payment_event, entity, razorpay_payment_id)

    if event_type in DISPUTE_EVENTS:
        return _handle_dispute(db, payment_event, entity, razorpay_payment_id)

    return None


def _handle_failure(db: Session, payment_event: PaymentEvent, entity: dict, razorpay_payment_id: str | None) -> RevenueCase:
    existing = None
    if razorpay_payment_id:
        existing = (
            db.query(RevenueCase)
            .filter(RevenueCase.razorpay_payment_id == razorpay_payment_id)
            .first()
        )

    if existing:
        # Same payment failing again (e.g. Razorpay's own native retry also
        # failed) — attach the event, don't spawn a duplicate case, but do
        # re-diagnose: the new failure may carry a more specific reason.
        payment_event.revenue_case_id = existing.id
        _log_audit(db, existing, "failure_event_received", {"event_id": payment_event.razorpay_event_id})
        _diagnose(db, existing, entity)
        return existing

    case = RevenueCase(
        source="razorpay",
        razorpay_payment_id=razorpay_payment_id,
        amount=_amount_in_rupees(entity) or Decimal("0"),
        currency=entity.get("currency", "INR"),
        state="DETECTED",
    )
    db.add(case)
    db.flush()  # get case.id before linking the event / audit row

    payment_event.revenue_case_id = case.id
    _log_audit(db, case, "case_detected", {"razorpay_payment_id": razorpay_payment_id})
    _diagnose(db, case, entity)
    return case


def _recover_case(db: Session, case: RevenueCase, reason: str, extra_detail: dict | None = None) -> RevenueCase:
    """Shared terminal-recovery logic: cancel anything pending, mark RECOVERED."""
    cancelled = (
        db.query(Action)
        .filter(Action.revenue_case_id == case.id, Action.status == "SCHEDULED")
        .all()
    )
    for action in cancelled:
        action.status = "CANCELLED"

    case.state = "RECOVERED"
    case.updated_at = datetime.utcnow()
    detail = {"reason": reason, "cancelled_actions": len(cancelled)}
    if extra_detail:
        detail.update(extra_detail)
    _log_audit(db, case, "case_recovered_silently", detail)
    return case


def _handle_recovery(db: Session, payment_event: PaymentEvent, entity: dict, razorpay_payment_id: str | None) -> RevenueCase | None:
    if not razorpay_payment_id:
        return None

    case = (
        db.query(RevenueCase)
        .filter(RevenueCase.razorpay_payment_id == razorpay_payment_id)
        .first()
    )
    if not case:
        # Recovery event with no matching failed case — nothing to do yet
        # (e.g. a payment that succeeded on the first try never opened a case).
        return None

    payment_event.revenue_case_id = case.id

    if case.state in ("RECOVERED", "STOPPED", "DISPUTED"):
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
    if not link_id:
        return None

    case = (
        db.query(RevenueCase)
        .filter(RevenueCase.razorpay_payment_link_id == link_id)
        .first()
    )
    if not case:
        payment_event.revenue_case_id = None
        return None

    payment_event.revenue_case_id = case.id

    if case.state in ("RECOVERED", "STOPPED", "DISPUTED"):
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
    case.updated_at = datetime.utcnow()
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
        .filter(RevenueCase.razorpay_payment_id == razorpay_payment_id)
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


def handle_customer_reply(db: Session, case: RevenueCase, message_body: str, now: datetime | None = None) -> RevenueCase:
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
    now = now or datetime.utcnow()

    extraction = llm_client.extract_ptp_intent(message_body, now=now)

    db.add(CustomerMessage(
        revenue_case_id=case.id,
        direction="inbound",
        channel="simulated" if extraction.simulated else "llm",
        body=message_body,
        extracted={
            "intent": extraction.intent,
            "amount": extraction.amount,
            "date": extraction.date,
            "confidence": extraction.confidence,
        },
    ))

    if case.state not in REPLYABLE_STATES:
        _log_audit(db, case, "customer_reply_ignored_terminal_state", {"state": case.state})
        db.flush()
        return case

    if extraction.intent == "dispute":
        _dispute_case(db, case, reason="customer_reported_dispute", extra_detail={"message": message_body[:200]})
        db.flush()
        return case

    outstanding = float(case.amount) if case.amount is not None else 0.0
    validation = ptp_extractor.validate_promise(extraction, outstanding_amount=outstanding, now=now)

    if not validation.valid:
        case.state = "HUMAN_REVIEW"
        case.updated_at = now
        _log_audit(
            db, case, "ptp_validation_failed",
            {"reason": validation.reason, "extraction": {"intent": extraction.intent, "amount": extraction.amount, "date": extraction.date, "confidence": extraction.confidence}},
        )
        db.flush()
        return case

    promise = PromiseToPay(
        revenue_case_id=case.id,
        promised_amount=validation.amount,
        promised_date=datetime.combine(validation.promised_date, datetime.min.time()),
        confidence=extraction.confidence,
        status="PENDING",
    )
    db.add(promise)
    db.flush()

    db.add(Action(
        revenue_case_id=case.id,
        action_type="FOLLOW_UP_PTP",
        status="SCHEDULED",
        scheduled_for=promise.promised_date,
    ))

    case.state = "AWAITING_OUTCOME"
    case.updated_at = now
    _log_audit(
        db, case, "promise_to_pay_recorded",
        {"promise_id": promise.id, "amount": float(promise.promised_amount), "promised_date": validation.promised_date.isoformat()},
    )
    db.flush()
    return case
