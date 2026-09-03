from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from app.core.database import get_db
from app.core.config import settings
from app.core.demo_auth import require_demo_admin
from app.models import RevenueCase, PromiseToPay
from app.services.task_queue import get_queue

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _resolve_counts(db: Session):
    # Only consider source=razorpay for operational metrics; exclude experiment/synthetic if any
    # But fallback to all if razorpay none? We include all non-experiment for at-risk? Use filter source='razorpay'.
    base = db.query(RevenueCase).filter(RevenueCase.source == "razorpay")
    total = base.count()
    # States
    open_states = ["DETECTED", "DIAGNOSED", "DECISION_READY", "WAITING", "ACTION_SCHEDULED", "AWAITING_OUTCOME", "HUMAN_REVIEW"]
    waiting_states = ["WAITING"]
    human_review_states = ["HUMAN_REVIEW"]
    recovered_states = ["RECOVERED"]

    open_count = db.query(func.count()).select_from(RevenueCase).filter(RevenueCase.source == "razorpay", RevenueCase.state.in_(open_states)).scalar() or 0
    waiting_count = db.query(func.count()).select_from(RevenueCase).filter(RevenueCase.source == "razorpay", RevenueCase.state.in_(waiting_states)).scalar() or 0
    human_review_count = db.query(func.count()).select_from(RevenueCase).filter(RevenueCase.source == "razorpay", RevenueCase.state.in_(human_review_states)).scalar() or 0
    recovered_count = db.query(func.count()).select_from(RevenueCase).filter(RevenueCase.source == "razorpay", RevenueCase.state.in_(recovered_states)).scalar() or 0
    # Also count stopped/disputed for completeness
    stopped_count = db.query(func.count()).select_from(RevenueCase).filter(RevenueCase.source == "razorpay", RevenueCase.state == "STOPPED").scalar() or 0
    disputed_count = db.query(func.count()).select_from(RevenueCase).filter(RevenueCase.source == "razorpay", RevenueCase.state == "DISPUTED").scalar() or 0

    # Revenue sums
    # Use func.coalesce to handle null
    at_risk = db.query(func.coalesce(func.sum(RevenueCase.amount), 0)).filter(RevenueCase.source == "razorpay", RevenueCase.state.in_(open_states)).scalar() or 0
    recovered_revenue = db.query(func.coalesce(func.sum(RevenueCase.amount), 0)).filter(RevenueCase.source == "razorpay", RevenueCase.state == "RECOVERED").scalar() or 0
    total_at_risk_all = db.query(func.coalesce(func.sum(RevenueCase.amount), 0)).filter(RevenueCase.source == "razorpay").scalar() or 0

    # Active PTPs
    active_ptps = db.query(func.count()).select_from(PromiseToPay).join(RevenueCase, PromiseToPay.revenue_case_id == RevenueCase.id).filter(PromiseToPay.status == "PENDING", RevenueCase.source == "razorpay").scalar() or 0

    # Counts by state
    rows = db.query(RevenueCase.state, func.count()).filter(RevenueCase.source == "razorpay").group_by(RevenueCase.state).all()
    by_state = {state or "UNKNOWN": count for state, count in rows}

    # Counts by failure category
    cat_rows = db.query(RevenueCase.failure_category, func.count()).filter(RevenueCase.source == "razorpay").group_by(RevenueCase.failure_category).all()
    by_category = {cat or "UNCLASSIFIED": count for cat, count in cat_rows}

    # Avg? also return floats safely
    def to_float(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    return {
        "total_cases": int(total),
        "open_cases": int(open_count),
        "recovered_cases": int(recovered_count),
        "waiting_cases": int(waiting_count),
        "human_review_cases": int(human_review_count),
        "stopped_cases": int(stopped_count),
        "disputed_cases": int(disputed_count),
        "active_ptps": int(active_ptps),
        "revenue_at_risk": to_float(at_risk),
        "revenue_recovered": to_float(recovered_revenue),
        "revenue_total": to_float(total_at_risk_all),
        "by_state": by_state,
        "by_category": by_category,
    }


@router.get("/summary")
def dashboard_summary(db: Session = Depends(get_db), _auth: bool = Depends(require_demo_admin)):
    counts = _resolve_counts(db)

    # Policy / model info
    from app.core.config import settings as cfg
    from app.ml import scorer

    policy_mode = (cfg.RECOVERY_POLICY or "baseline").strip().lower()
    if policy_mode not in ("baseline", "shadow", "adaptive"):
        policy_mode = "baseline"

    model_available = False
    model_version = None
    fingerprint = None
    fingerprint_short = None
    feature_schema_compatible = False
    try:
        info = scorer.get_model_info()
        model_available = bool(info.get("available"))
        model_version = info.get("model_version")
        fingerprint = info.get("fingerprint")
        fingerprint_short = info.get("fingerprint_short")
        feature_schema_compatible = bool(info.get("available"))
    except Exception:
        pass

    # Queue readiness
    redis_status = "disabled"
    queue_name = cfg.RQ_QUEUE_NAME if cfg.TASK_QUEUE_ENABLED else "disabled"
    if cfg.TASK_QUEUE_ENABLED:
        try:
            get_queue().connection.ping()
            redis_status = "connected"
        except Exception:
            redis_status = "unreachable"

    # Razorpay integration
    razorpay_status = {
        "api_enabled": bool(cfg.RAZORPAY_API_ENABLED),
        "key_configured": bool(cfg.RAZORPAY_KEY_ID and cfg.RAZORPAY_KEY_SECRET),
        "is_test_mode": bool(cfg.RAZORPAY_KEY_ID.startswith("rzp_test_")) if cfg.RAZORPAY_KEY_ID else False,
        "mode_label": "RAZORPAY TEST MODE" if (cfg.RAZORPAY_API_ENABLED and cfg.RAZORPAY_KEY_ID.startswith("rzp_test_")) else ("SIMULATED LINK" if not cfg.RAZORPAY_API_ENABLED else "DISABLED"),
        "simulated": not cfg.RAZORPAY_API_ENABLED,
    }

    # LLM
    from app.services import llm_client as _llm
    llm_info = {
        "enabled": bool(settings.LLM_API_ENABLED),
        "provider": settings.LLM_PROVIDER,
        "configured": bool(settings.LLM_API_KEY),
        "model": getattr(settings, "LLM_MODEL", _llm.ANTHROPIC_MODEL),
        "message_drafting": "available" if (settings.LLM_API_ENABLED and getattr(settings, "LLM_MESSAGE_DRAFT_ENABLED", True) and settings.LLM_API_KEY) else ("disabled" if not settings.LLM_API_ENABLED else "unavailable"),
        "ptp_extraction": "available" if (settings.LLM_API_ENABLED and getattr(settings, "LLM_PTP_EXTRACTION_ENABLED", True) and settings.LLM_API_KEY) else ("disabled" if not settings.LLM_API_ENABLED else "unavailable"),
        "prompt_versions": {
            "ptp_extraction": _llm.PTP_EXTRACTION_PROMPT_VERSION,
            "message_draft": _llm.MESSAGE_DRAFT_PROMPT_VERSION,
        },
        "schema_versions": {
            "ptp": _llm.PTP_SCHEMA_VERSION,
            "message": _llm.MESSAGE_DRAFT_SCHEMA_VERSION,
        },
    }

    # Database
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    finally:
        db.rollback()

    # Friction profile
    from app.ml.friction import PROFILE_WEIGHTS
    friction_profile = (cfg.ADAPTIVE_POLICY_PROFILE or "balanced")
    if friction_profile not in PROFILE_WEIGHTS and cfg.ADAPTIVE_FRICTION_WEIGHT is None:
        friction_profile = "balanced"
    if cfg.ADAPTIVE_FRICTION_WEIGHT is not None:
        friction_weight = float(cfg.ADAPTIVE_FRICTION_WEIGHT)
        friction_profile_label = f"custom:{friction_weight}"
    else:
        friction_weight = float(PROFILE_WEIGHTS.get(friction_profile, PROFILE_WEIGHTS["balanced"]))
        friction_profile_label = friction_profile

    return {
        "revenue_at_risk": counts["revenue_at_risk"],
        "revenue_recovered": counts["revenue_recovered"],
        "revenue_total": counts["revenue_total"],
        "total_cases": counts["total_cases"],
        "open_cases": counts["open_cases"],
        "recovered_cases": counts["recovered_cases"],
        "waiting_cases": counts["waiting_cases"],
        "human_review_cases": counts["human_review_cases"],
        "stopped_cases": counts["stopped_cases"],
        "disputed_cases": counts["disputed_cases"],
        "active_ptps": counts["active_ptps"],
        "by_state": counts["by_state"],
        "by_category": counts["by_category"],
        "policy_mode": policy_mode,
        "model": {
            "available": model_available,
            "version": model_version,
            "fingerprint": fingerprint,
            "fingerprint_short": fingerprint_short,
            "feature_schema_compatible": feature_schema_compatible,
        },
        "friction": {
            "profile": friction_profile_label,
            "weight": friction_weight,
        },
        "queue": {
            "status": redis_status,
            "name": queue_name,
            "enabled": bool(cfg.TASK_QUEUE_ENABLED),
        },
        "razorpay": razorpay_status,
        "llm": llm_info,
        "database": "connected" if db_ok else "unreachable",
    }
