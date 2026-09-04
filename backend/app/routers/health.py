from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.services.task_queue import get_queue

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """
    Liveness + sanitized readiness (backward compatible).
    Returns ok as long as process is alive; database/redis status reported.
    Never exposes secrets / connection strings.
    """
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    finally:
        try:
            db.rollback()
        except Exception:
            pass

    redis_status = "disabled"
    if settings.TASK_QUEUE_ENABLED:
        try:
            get_queue().connection.ping()
            redis_status = "connected"
        except Exception:
            redis_status = "unreachable"

    # Adaptive policy readiness — no provider calls, no secrets
    from app.core.config import settings as cfg
    from app.ml import scorer

    policy_mode = (cfg.RECOVERY_POLICY or "baseline").strip().lower()
    if policy_mode not in ("baseline", "shadow", "adaptive"):
        policy_mode = "baseline"
    adaptive_info = {
        "configured_mode": policy_mode,
        "model_available": False,
        "model_version": None,
        "fingerprint": None,
        "fingerprint_short": None,
        "feature_schema_compatible": False,
    }
    try:
        info = scorer.get_model_info()
        adaptive_info["model_available"] = bool(info.get("available"))
        adaptive_info["model_version"] = info.get("model_version")
        adaptive_info["fingerprint"] = info.get("fingerprint")
        adaptive_info["fingerprint_short"] = info.get("fingerprint_short")
        adaptive_info["feature_schema_compatible"] = bool(info.get("available"))
    except Exception:
        pass

    # LLM readiness — sanitized, no secrets, no provider call
    from app.services import llm_client as _llm

    # Provider-aware effective model (supports anthropic and opencode_zen)
    try:
        _eff_model = _llm._get_effective_model()  # type: ignore[attr-defined]
    except Exception:
        _eff_model = getattr(settings, "LLM_MODEL", _llm.ANTHROPIC_MODEL)

    llm_info = {
        "enabled": bool(settings.LLM_API_ENABLED),
        "provider": settings.LLM_PROVIDER,
        "configured": bool(settings.LLM_API_KEY),
        "model": _eff_model,
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
    overall = "ok" if db_ok and redis_status != "unreachable" else "degraded"

    return {
        "status": overall,
        "service": "recoveryos-backend",
        "database": "connected" if db_ok else "unreachable",
        "redis": redis_status,
        "queue": settings.RQ_QUEUE_NAME if settings.TASK_QUEUE_ENABLED else "disabled",
        "adaptive_policy": adaptive_info,
        "llm": llm_info,
        "version": "0.1.0",
    }


@router.get("/live")
def liveness():
    """Kubernetes-style liveness — always 200 if process is up. No DB check."""
    return {"status": "ok", "service": "recoveryos-backend"}


@router.get("/ready")
def readiness(db: Session = Depends(get_db), response: Response = None):
    """
    Readiness: checks database, Redis, queue, ML model availability.
    Returns 200 if ready, 503 if not ready. Optional providers (LLM, Razorpay
    Test Mode) never make the service unhealthy — they are reported only.
    No credentials or connection strings are ever returned.
    """
    checks: dict = {}
    ready = True

    # Database
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
        db.execute(text("SELECT 1 FROM revenue_cases LIMIT 1"))
        checks["migrations"] = "ok"
    except Exception as exc:
        db_ok = False
        ready = False
        checks["migrations"] = "unknown"
        checks["database_error"] = type(exc).__name__
    finally:
        try:
            db.rollback()
        except Exception:
            pass
    checks["database"] = "connected" if db_ok else "unreachable"

    # Redis / queue
    redis_status = "disabled"
    if settings.TASK_QUEUE_ENABLED:
        try:
            get_queue().connection.ping()
            redis_status = "connected"
        except Exception:
            redis_status = "unreachable"
            ready = False
    checks["redis"] = redis_status
    checks["queue"] = settings.RQ_QUEUE_NAME if settings.TASK_QUEUE_ENABLED else "disabled"

    # Adaptive policy
    from app.core.config import settings as cfg
    from app.ml import scorer

    policy_mode = (cfg.RECOVERY_POLICY or "baseline").strip().lower()
    if policy_mode not in ("baseline", "shadow", "adaptive"):
        policy_mode = "baseline"
    adaptive_info = {
        "configured_mode": policy_mode,
        "model_available": False,
        "model_version": None,
        "fingerprint": None,
        "fingerprint_short": None,
        "feature_schema_compatible": False,
    }
    try:
        info = scorer.get_model_info()
        adaptive_info["model_available"] = bool(info.get("available"))
        adaptive_info["model_version"] = info.get("model_version")
        adaptive_info["fingerprint"] = info.get("fingerprint")
        adaptive_info["fingerprint_short"] = info.get("fingerprint_short")
        adaptive_info["feature_schema_compatible"] = bool(info.get("available"))
        if policy_mode == "adaptive" and not info.get("available"):
            checks["adaptive"] = "model unavailable — will fallback to baseline"
        else:
            checks["adaptive"] = "ok" if info.get("available") else "model not required"
    except Exception:
        adaptive_info["model_available"] = False
        checks["adaptive"] = "error"

    from app.services import llm_client as _llm

    try:
        _eff_model2 = _llm._get_effective_model()  # type: ignore[attr-defined]
    except Exception:
        _eff_model2 = getattr(settings, "LLM_MODEL", _llm.ANTHROPIC_MODEL)

    llm_info = {
        "enabled": bool(settings.LLM_API_ENABLED),
        "provider": settings.LLM_PROVIDER,
        "configured": bool(settings.LLM_API_KEY),
        "model": _eff_model2,
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

    razorpay_info = {
        "api_enabled": bool(cfg.RAZORPAY_API_ENABLED),
        "configured": bool(cfg.RAZORPAY_KEY_ID and cfg.RAZORPAY_KEY_SECRET),
        "is_test_mode": bool(cfg.RAZORPAY_KEY_ID.startswith("rzp_test_")) if cfg.RAZORPAY_KEY_ID else False,
        "mode_label": "RAZORPAY TEST MODE" if (cfg.RAZORPAY_API_ENABLED and cfg.RAZORPAY_KEY_ID.startswith("rzp_test_")) else ("SIMULATED" if not cfg.RAZORPAY_API_ENABLED else "DISABLED"),
    }

    status_code = 200 if ready else 503
    if response is not None:
        response.status_code = status_code

    return {
        "status": "ok" if ready else "not_ready",
        "ready": ready,
        "service": "recoveryos-backend",
        "checks": checks,
        "adaptive_policy": adaptive_info,
        "llm": llm_info,
        "razorpay": razorpay_info,
    }
