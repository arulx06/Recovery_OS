from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.core.demo_auth import require_demo_admin
from app.core.time import utc_now, utc_to_local
from app.models import RevenueCase, Decision, Action, AuditEvent, PromiseToPay, CustomerMessage, PaymentEvent
from app.services import llm_client, orchestrator, task_queue
from app.services.explainability import (
    build_timeline,
    failure_explanation,
    friction_breakdown,
    guardrail_visibility,
    normalize_decision,
    provider_truth_summary,
)

router = APIRouter(prefix="/cases", tags=["cases"])


class CustomerReplyRequest(BaseModel):
    body: str

    @field_validator("body")
    @classmethod
    def _validate_body(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("body must be non-empty")
        if len(v) > 2000:
            raise ValueError("body too long — max 2000 chars")
        return v.strip()


def _latest_decision(db: Session, case_id: str) -> Decision | None:
    return (
        db.query(Decision)
        .filter(Decision.revenue_case_id == case_id)
        .order_by(Decision.created_at.desc(), Decision.id.desc())
        .first()
    )


@router.get("")
def list_cases(
    _auth: bool = Depends(require_demo_admin),
    db: Session = Depends(get_db),
    state: str | None = Query(default=None, description="Filter by case state"),
    failure_category: str | None = Query(default=None, description="Filter by failure category"),
    chosen_action: str | None = Query(default=None, description="Filter by latest chosen action"),
    policy_mode: str | None = Query(default=None, description="Filter by policy mode baseline|shadow|adaptive|adaptive_fallback"),
    search: str | None = Query(default=None, description="Search by payment/case/link id fragment"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """
    Returns recent revenue cases with their current state, diagnosed
    failure category, and — from Phase 3 on — the most recent decision the
    policy engine made for each one.
    Supports server-side filtering, search, and pagination to avoid browser-side
    mass filtering.
    """
    # Base query: operational razorpay cases only; experiment cases remain offline
    q = db.query(RevenueCase).filter(RevenueCase.source == "razorpay")

    if state:
        q = q.filter(RevenueCase.state == state)
    if failure_category:
        q = q.filter(RevenueCase.failure_category == failure_category)
    if search:
        s = f"%{search}%"
        q = q.filter(
            or_(
                RevenueCase.id.ilike(s),
                RevenueCase.razorpay_payment_id.ilike(s),
                RevenueCase.razorpay_payment_link_id.ilike(s),
                RevenueCase.razorpay_subscription_id.ilike(s),
            )
        )

    # Filter against the latest persisted Decision before pagination. The
    # correlated subquery avoids both an arbitrary candidate window and a
    # join that can duplicate cases with historical decisions.
    if chosen_action or policy_mode:
        latest_decision_id = (
            select(Decision.id)
            .where(Decision.revenue_case_id == RevenueCase.id)
            .order_by(Decision.created_at.desc(), Decision.id.desc())
            .limit(1)
            .correlate(RevenueCase)
            .scalar_subquery()
        )
        q = q.join(Decision, Decision.id == latest_decision_id)
        if chosen_action:
            q = q.filter(Decision.chosen_action == chosen_action)
        if policy_mode:
            q = q.filter(func.coalesce(Decision.policy_mode, "baseline") == policy_mode)

    cases = (
        q.order_by(RevenueCase.created_at.desc(), RevenueCase.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    result = []
    for c in cases:
        decision = _latest_decision(db, c.id)
        # Latest action for operational columns
        latest_action = (
            db.query(Action)
            .filter(Action.revenue_case_id == c.id)
            .order_by(Action.created_at.desc())
            .first()
        )
        # Pending promise check
        pending_ptp = (
            db.query(PromiseToPay.id)
            .filter(PromiseToPay.revenue_case_id == c.id, PromiseToPay.status == "PENDING")
            .first()
        )
        # Friction indicator: show score from latest adaptive decision if available
        friction_score = None
        if decision and decision.alternatives and isinstance(decision.alternatives, dict):
            chosen_meta = decision.alternatives.get(decision.chosen_action) if decision.chosen_action else None
            if isinstance(chosen_meta, dict):
                try:
                    friction_score = float(chosen_meta.get("friction_score")) if chosen_meta.get("friction_score") is not None else None
                except Exception:
                    friction_score = None
        # Contact count
        contact_count = (
            db.query(Action.id)
            .filter(
                Action.revenue_case_id == c.id,
                Action.action_type.in_(["CONTACT_CUSTOMER", "CREATE_PAYMENT_LINK", "COLLECT_PROMISE_TO_PAY"]),
                Action.status.in_(("SCHEDULED", "EXECUTED")),
            )
            .count()
        )
        result.append({
            "id": c.id,
            "amount": float(c.amount) if c.amount is not None else None,
            "currency": c.currency,
            "state": c.state,
            "failure_category": c.failure_category,
            "error_source": c.error_source,
            "error_step": c.error_step,
            "error_reason": c.error_reason,
            "chosen_action": decision.chosen_action if decision else None,
            "policy_mode": getattr(decision, "policy_mode", None) if decision else None,
            "friction_profile": getattr(decision, "friction_profile", None) if decision else None,
            "friction_score": friction_score,
            "latest_action_type": latest_action.action_type if latest_action else None,
            "latest_action_status": latest_action.status if latest_action else None,
            "payment_link_state": "SIMULATED" if (latest_action and latest_action.action_type == "CREATE_PAYMENT_LINK" and latest_action.result and latest_action.result.get("simulated")) else ("TEST_MODE" if c.razorpay_payment_link_id and not (latest_action and latest_action.result and latest_action.result.get("simulated")) else None),
            "ptp_status": "PENDING" if pending_ptp else None,
            "contact_count": contact_count,
            "razorpay_payment_id": c.razorpay_payment_id,
            "razorpay_subscription_id": c.razorpay_subscription_id,
            "razorpay_payment_link_id": c.razorpay_payment_link_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        })
    return result


@router.get("/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db), _auth: bool = Depends(require_demo_admin)):
    """
    Full detail for one case: every decision made (with alternatives and
    the guardrails applied), every action scheduled/executed, and the
    append-only audit trail. This is the "why did it do that" view.
    Enriched with derived explainability models and chronological timeline.
    """
    case = db.query(RevenueCase).filter(RevenueCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="case not found")

    decisions = (
        db.query(Decision)
        .filter(Decision.revenue_case_id == case_id)
        .order_by(Decision.created_at, Decision.id)
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
    payment_events = (
        db.query(PaymentEvent)
        .filter(PaymentEvent.revenue_case_id == case_id)
        .order_by(PaymentEvent.received_at)
        .all()
    )

    # Derived explainability
    fail_exp = failure_explanation(case)
    decision_inspectors = [normalize_decision(d) for d in decisions]
    guardrails = guardrail_visibility(db, case)
    # friction for latest decision
    latest_decision = decisions[-1] if decisions else None
    friction = friction_breakdown(db, case, latest_decision)
    timeline = build_timeline(case, decisions, actions, messages, promises, audit_events, payment_events)
    provider_truth = provider_truth_summary(case, actions, audit_events)

    # Shadow info from audit
    shadow_events = [e for e in audit_events if e.event == "shadow_adaptive_recommendation"]
    shadow_summary = None
    if shadow_events:
        se = shadow_events[-1]
        detail = se.detail or {}
        shadow_summary = {
            "baseline_chosen": detail.get("baseline_chosen"),
            "adaptive_suggested": detail.get("adaptive_suggested"),
            "disagreement": detail.get("disagreement"),
            "fingerprint": detail.get("fingerprint"),
            "friction_profile": detail.get("friction_profile"),
            "candidates": detail.get("candidates"),
            "adaptive_utility": detail.get("adaptive_utility"),
        }

    # Adaptive fallback
    fallback_events = [e for e in audit_events if e.event == "adaptive_fallback"]
    fallback_summary = None
    if fallback_events:
        fallback_summary = {"reason": (fallback_events[-1].detail or {}).get("reason")}

    # Human review reason — first audit that caused it
    human_review_reason = None
    if case.state == "HUMAN_REVIEW":
        # find last relevant audit
        for ev in reversed(audit_events):
            if ev.event in ("ptp_validation_failed", "ptp_validation_rejected", "ptp_extraction_uncertain", "payment_link_reconciliation_failed", "payment_link_manual_review_required", "action_execution_failed", "customer_payment_claim_received"):
                human_review_reason = {"event": ev.event, "detail": ev.detail}
                break
        if not human_review_reason:
            human_review_reason = {"event": "human_review", "detail": {"reason": "Guardrail or policy routed to human review"}}

    latest_action = actions[-1] if actions else None

    return {
        "id": case.id,
        "amount": float(case.amount) if case.amount is not None else None,
        "currency": case.currency,
        "state": case.state,
        "failure_category": case.failure_category,
        "error_source": case.error_source,
        "error_step": case.error_step,
        "error_reason": case.error_reason,
        "razorpay_payment_id": case.razorpay_payment_id,
        "razorpay_subscription_id": case.razorpay_subscription_id,
        "razorpay_payment_link_id": case.razorpay_payment_link_id,
        "source": case.source,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        # Enriched / derived (read-only view models)
        "failure_explanation": fail_exp,
        "guardrails": guardrails,
        "friction": friction,
        "explainability_scopes": {
            "guardrails": "current_case_state",
            "friction": "current_case_state",
            "decision_inspectors": "persisted_decision_time",
        },
        "provider_truth": provider_truth,
        "timeline": timeline,
        "shadow": shadow_summary,
        "adaptive_fallback": fallback_summary,
        "human_review_reason": human_review_reason,
        "latest_action": {
            "id": latest_action.id,
            "action_type": latest_action.action_type,
            "status": latest_action.status,
            "scheduled_for": latest_action.scheduled_for.isoformat() if latest_action.scheduled_for else None,
            "executed_at": latest_action.executed_at.isoformat() if latest_action.executed_at else None,
        } if latest_action else None,
        "decisions": [
            {
                "id": d.id,
                "chosen_action": d.chosen_action,
                "expected_value": float(d.expected_value) if d.expected_value is not None else None,
                "alternatives": d.alternatives,
                "guardrails_applied": d.guardrails_applied,
                "explanation": d.explanation,
                "policy_mode": getattr(d, "policy_mode", None),
                "model_version": getattr(d, "model_version", None),
                "model_fingerprint": getattr(d, "model_fingerprint", None),
                "friction_profile": getattr(d, "friction_profile", None),
                "friction_weight": float(d.friction_weight) if getattr(d, "friction_weight", None) is not None else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
        "decision_inspectors": decision_inspectors,
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
                "created_at": a.created_at.isoformat() if a.created_at else None,
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
                "generation_method": getattr(m, "generation_method", None),
                "llm_provider": getattr(m, "llm_provider", None),
                "llm_model": getattr(m, "llm_model", None),
                "prompt_version": getattr(m, "prompt_version", None),
                "schema_version": getattr(m, "schema_version", None),
                "status": getattr(m, "status", None),
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
                "extraction_method": getattr(p, "extraction_method", None),
                "llm_provider": getattr(p, "llm_provider", None),
                "llm_model": getattr(p, "llm_model", None),
                "prompt_version": getattr(p, "prompt_version", None),
                "schema_version": getattr(p, "schema_version", None),
                "amount_method": getattr(p, "amount_method", None),
                "reasoning_code": getattr(p, "reasoning_code", None),
                "source_message_id": getattr(p, "source_message_id", None),
                "created_at": p.created_at.isoformat() if p.created_at else None,
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
        "payment_events": [
            {
                "id": pe.id,
                "event_type": pe.event_type,
                "razorpay_event_id": pe.razorpay_event_id,
                "razorpay_payment_id": pe.razorpay_payment_id,
                "razorpay_payment_link_id": pe.razorpay_payment_link_id,
                "received_at": pe.received_at.isoformat() if pe.received_at else None,
            }
            for pe in payment_events
        ],
    }


@router.post("/{case_id}/customer-reply")
def customer_reply(case_id: str, body: CustomerReplyRequest, request: Request, db: Session = Depends(get_db), _auth: bool = Depends(require_demo_admin)):
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
    # Extraction runs outside DB transaction (no lock held during LLM call)
    # The wrapper catches only typed provider errors; unexpected errors still propagate.
    extraction = llm_client.extract_with_fallback(body.body, now=business_now)
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
