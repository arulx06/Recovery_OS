from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.services.task_queue import get_queue

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """
    Day 1 exit criteria: judges (or a reviewer) should be able to hit this
    and immediately know the backend is up AND the database is reachable.
    """
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    finally:
        db.rollback()

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
        if policy_mode == "baseline" and not info.get("available"):
            # baseline mode: model unavailable is not unhealthy
            pass
    except Exception:
        pass

    # Determine overall status: baseline fallback always available, so adaptive degraded is not "degraded" for baseline
    overall = "ok" if db_ok and redis_status != "unreachable" else "degraded"

    return {
        "status": overall,
        "service": "recoveryos-backend",
        "database": "connected" if db_ok else "unreachable",
        "redis": redis_status,
        "queue": settings.RQ_QUEUE_NAME if settings.TASK_QUEUE_ENABLED else "disabled",
        "adaptive_policy": adaptive_info,
    }
