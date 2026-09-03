"""Admin / demo reset — protected by demo admin token when enabled.

Provides safe, scoped demo reset that only touches demo-prefixed rows.
"""
from fastapi import APIRouter, Depends

from app.core.database import SessionLocal
from app.core.demo_auth import require_demo_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/demo-reset")
def demo_reset(_auth: bool = Depends(require_demo_admin)):
    """Delete only demo-prefixed rows (pay_demo_* / sub_demo_*) and return count.

    Safe: never deletes non-demo revenue_cases. Uses same scoped delete order
    as scripts/seed_demo.py.
    """
    # Reuse seed_demo reset logic but inline to avoid import side-effects
    from app.models import Action, AuditEvent, CustomerMessage, Decision, ExperimentCase, PaymentEvent, PromiseToPay, RevenueCase

    DEMO_IDS = [
        "pay_demo_A_auth_001",
        "pay_demo_B_link_001",
        "pay_demo_C_wait_001",
        "pay_demo_C_native_001",
        "pay_demo_D_adaptive_001",
        "pay_demo_E_injection_001",
        "pay_demo_F_shadow_001",
    ]
    with SessionLocal.begin() as db:
        demo_cases = db.query(RevenueCase).filter(
            RevenueCase.source == "razorpay",
            RevenueCase.razorpay_payment_id.in_(DEMO_IDS),
        ).all()
        demo_ids = [c.id for c in demo_cases]
        if not demo_ids:
            return {"status": "ok", "deleted_cases": 0, "message": "no demo cases present"}
        db.query(AuditEvent).filter(AuditEvent.revenue_case_id.in_(demo_ids)).delete(synchronize_session=False)
        db.query(Action).filter(Action.revenue_case_id.in_(demo_ids)).delete(synchronize_session=False)
        db.query(PromiseToPay).filter(PromiseToPay.revenue_case_id.in_(demo_ids)).delete(synchronize_session=False)
        db.query(CustomerMessage).filter(CustomerMessage.revenue_case_id.in_(demo_ids)).delete(synchronize_session=False)
        db.query(Decision).filter(Decision.revenue_case_id.in_(demo_ids)).delete(synchronize_session=False)
        db.query(ExperimentCase).filter(ExperimentCase.revenue_case_id.in_(demo_ids)).delete(synchronize_session=False)
        db.query(PaymentEvent).filter(PaymentEvent.revenue_case_id.in_(demo_ids)).delete(synchronize_session=False)
        db.query(RevenueCase).filter(RevenueCase.id.in_(demo_ids)).delete(synchronize_session=False)
    return {"status": "ok", "deleted_cases": len(demo_ids), "deleted_payment_ids": DEMO_IDS}


@router.get("/demo-check")
def demo_check(_auth: bool = Depends(require_demo_admin)):
    """Return current demo cases without modifying DB."""
    from app.models import RevenueCase, Decision, Action, PromiseToPay

    DEMO_IDS = [
        "pay_demo_A_auth_001",
        "pay_demo_B_link_001",
        "pay_demo_C_wait_001",
        "pay_demo_C_native_001",
        "pay_demo_D_adaptive_001",
        "pay_demo_E_injection_001",
        "pay_demo_F_shadow_001",
    ]
    with SessionLocal() as db:
        rows = db.query(RevenueCase).filter(RevenueCase.source == "razorpay", RevenueCase.razorpay_payment_id.in_(DEMO_IDS)).all()
        result = []
        for c in rows:
            decisions = db.query(Decision).filter(Decision.revenue_case_id == c.id).all()
            actions = db.query(Action).filter(Action.revenue_case_id == c.id).all()
            ptps = db.query(PromiseToPay).filter(PromiseToPay.revenue_case_id == c.id).all()
            result.append({
                "razorpay_payment_id": c.razorpay_payment_id,
                "state": c.state,
                "failure_category": c.failure_category,
                "amount": float(c.amount) if c.amount else None,
                "decisions": [d.chosen_action for d in decisions],
                "policy_modes": [d.policy_mode for d in decisions],
                "actions": [f"{a.action_type}:{a.status}" for a in actions],
                "ptps": len(ptps),
                "link_id": c.razorpay_payment_link_id,
            })
        db.rollback()
    return {"demo_cases": result, "count": len(result)}
