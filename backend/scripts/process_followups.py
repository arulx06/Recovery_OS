#!/usr/bin/env python3
"""
Process any Promise-to-Pay follow-ups whose due date has passed.

Run this periodically (cron, or manually while testing) — there's no live
scheduler wired up yet, so nothing calls this automatically. See
app/services/ptp_followup.py for what "processing" means.

Usage:
    python scripts/process_followups.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal
from app.services.ptp_followup import process_due_followups

if __name__ == "__main__":
    db = SessionLocal()
    summary = process_due_followups(db)
    db.commit()
    db.close()

    print(f"Processed {summary['processed']} due follow-up(s):")
    print(f"  already recovered by another path: {summary['already_recovered']}")
    print(f"  broken promise -> escalated to human review: {summary['broken']}")
