from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.core.time import utc_now, utc_to_local
from app.models import RevenueCase, Decision, Action, AuditEvent, PromiseToPay, CustomerMessage
from app.services import llm_client, orchestrator, task_queue

router = APIRouter(prefix="/cases", tags=["cases"])


class CustomerReplyRequest(BaseModel):
    body: str


def _latest_decision(db: Session, case_id: str) -> Decision | None:
    return (
        db.query(Decision)
        .filter(Decision.revenue_case_id == case_id)
        .order_by(Decision.created_at.desc())
        .first()
    )


@router.get("")
def list_cases(db: Session = Depends(get_db)):
    """
    Returns recent revenue cases with their current state, diagnosed
    failure category, and — from Phase 3 on — the most recent decision the
    policy engine made for each one.
    """
    cases = db.query(RevenueCase).order_by(RevenueCase.created_at.desc()).limit(50).all()
    result = []
    for c in cases:
        decision = _latest_decision(db, c.id)
        result.append({
            "id": c.id,
            "amount": float(c.amount) if c.amount is not None else None,
            "state": c.state,
            "failure_category": c.failure_category,
            "error_source": c.error_source,
            "error_reason": c.error_reason,
            "chosen_action": decision.chosen_action if decision else None,
            "razorpay_payment_link_id": c.razorpay_payment_link_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
    return result


@router.get("/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db)):
    """
    Full detail for one case: every decision made (with alternatives and
    the guardrails applied), every action scheduled/executed, and the
    append-only audit trail. This is the "why did it do that" view.
    """
    case = db.query(RevenueCase).filter(RevenueCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="case not found")

    decisions = (
        db.query(Decision)
        .filter(Decision.revenue_case_id == case_id)
        .order_by(Decision.created_at)
        .all()
    )
    actions = (
        db.query(Action)
        .filter(Action.revenue_case_id == case_id)
        .order_by(Action.created_at)
        .all()
    )
    audit_events = (
        db.query(AuditEvent)
        .filter(AuditEvent.revenue_case_id == case_id)
        .order_by(AuditEvent.created_at)
        .all()
    )

    messages = (
        db.query(CustomerMessage)
        .filter(CustomerMessage.revenue_case_id == case_id)
        .order_by(CustomerMessage.created_at)
        .all()
    )
    promises = (
        db.query(PromiseToPay)
        .filter(PromiseToPay.revenue_case_id == case_id)
        .order_by(PromiseToPay.created_at)
        .all()
    )

    return {
        "id": case.id,
        "amount": float(case.amount) if case.amount is not None else None,
        "state": case.state,
        "failure_category": case.failure_category,
        "error_source": case.error_source,
        "error_step": case.error_step,
        "error_reason": case.error_reason,
        "razorpay_payment_id": case.razorpay_payment_id,
        "razorpay_payment_link_id": case.razorpay_payment_link_id,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        "decisions": [
            {
                "id": d.id,
                "chosen_action": d.chosen_action,
                "expected_value": float(d.expected_value) if d.expected_value is not None else None,
                "alternatives": d.alternatives,
                "guardrails_applied": d.guardrails_applied,
                "explanation": d.explanation,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
        "actions": [
            {
                "id": a.id,
                "action_type": a.action_type,
                "status": a.status,
                "scheduled_for": a.scheduled_for.isoformat() if a.scheduled_for else None,
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
                "result": a.result,
                "promise_to_pay_id": a.promise_to_pay_id,
                "attempt_count": a.attempt_count,
                "max_attempts": a.max_attempts,
                "claimed_at": a.claimed_at.isoformat() if a.claimed_at else None,
                "enqueued_at": a.enqueued_at.isoformat() if a.enqueued_at else None,
                "queue_job_id": a.queue_job_id,
                "last_error": a.last_error,
            }
            for a in actions
        ],
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "channel": m.channel,
                "body": m.body,
                "extracted": m.extracted,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
        "promises_to_pay": [
            {
                "id": p.id,
                "promised_amount": float(p.promised_amount) if p.promised_amount is not None else None,
                "promised_date": p.promised_date.isoformat() if p.promised_date else None,
                "confidence": float(p.confidence) if p.confidence is not None else None,
                "status": p.status,
            }
            for p in promises
        ],
        "audit_trail": [
            {
                "event": e.event,
                "detail": e.detail,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in audit_events
        ],
    }


@router.post("/{case_id}/customer-reply")
def customer_reply(case_id: str, body: CustomerReplyRequest, db: Session = Depends(get_db)):
    """
    Phase 6: simulates receiving an inbound customer reply (SMS/WhatsApp/
    email — whichever channel eventually sends this in production isn't
    RecoveryOS's concern; this is the ingestion point regardless of
    transport). Runs PTP/dispute extraction and returns the resulting case
    state.
    """
    exists = db.query(RevenueCase.id).filter(RevenueCase.id == case_id).scalar()
    db.rollback()
    if not exists:
        raise HTTPException(status_code=404, detail="case not found")

    now = utc_now()
    business_now = utc_to_local(now, settings.MERCHANT_TIMEZONE)
    extraction = llm_client.extract_ptp_intent(body.body, now=business_now)
    case = (
        db.query(RevenueCase)
        .filter(RevenueCase.id == case_id)
        .with_for_update()
        .first()
    )
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    updated = orchestrator.handle_customer_reply(
        db, case, body.body, now=now, business_now=business_now, extraction=extraction,
    )
    updated_id = updated.id
    updated_state = updated.state
    db.commit()
    task_queue.enqueue_case_actions(updated_id)

    return {"case_id": updated_id, "case_state": updated_state}
