from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db

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

    return {
        "status": "ok" if db_ok else "degraded",
        "service": "recoveryos-backend",
        "database": "connected" if db_ok else "unreachable",
    }
