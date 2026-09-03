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

    return {
        "status": "ok" if db_ok and redis_status != "unreachable" else "degraded",
        "service": "recoveryos-backend",
        "database": "connected" if db_ok else "unreachable",
        "redis": redis_status,
        "queue": settings.RQ_QUEUE_NAME if settings.TASK_QUEUE_ENABLED else "disabled",
    }
