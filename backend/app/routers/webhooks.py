"""
Webhook Gateway.

receive webhook -> verify signature -> extract x-razorpay-event-id ->
already processed? (yes -> 200, ignore) -> store immutable event ->
update current entity state -> trigger recovery orchestration -> return 200

Two things matter more than they look like they should:

1. We always return 200 once the signature is valid, even if something
   downstream is degraded — Razorpay retries on non-2xx, and repeated
   retries of an event we've already stored just get deduped anyway. The
   one case we return non-200 for is a bad signature, which should never
   happen from a real Razorpay delivery.

2. Idempotency is enforced at the database level (payment_events.
   razorpay_event_id is unique), not just checked-then-inserted in
   application code, so a race between two near-simultaneous deliveries
   of the same event can't create two rows.
"""
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import json

from app.core.config import settings
from app.core.database import get_db
from app.core.security import verify_signature
from app.models import PaymentEvent
from app.services import orchestrator

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")

    if not verify_signature(raw_body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed JSON body")

    event_type = payload.get("event", "unknown")
    razorpay_event_id = request.headers.get("x-razorpay-event-id")

    payment_event = PaymentEvent(
        razorpay_event_id=razorpay_event_id,
        event_type=event_type,
        raw_payload=payload,
    )
    db.add(payment_event)

    try:
        db.flush()  # trip the unique constraint here if this event_id was seen before
    except IntegrityError:
        db.rollback()
        return {"status": "ignored", "reason": "duplicate_event", "event_id": razorpay_event_id}

    case = orchestrator.handle_event(db, payment_event, event_type, payload)
    db.commit()

    return {
        "status": "processed",
        "event_type": event_type,
        "case_id": case.id if case else None,
        "case_state": case.state if case else None,
    }
