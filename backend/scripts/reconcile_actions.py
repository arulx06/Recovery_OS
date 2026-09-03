#!/usr/bin/env python3
"""Recover expired action claims and republish durable scheduled actions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.temporal_runtime import reconcile_actions


if __name__ == "__main__":
    summary = reconcile_actions()
    print(f"Recovered {summary['reset']} expired claim(s); {summary['failed']} exhausted.")
    print(f"Found {summary['scheduled']} scheduled action(s); enqueued {summary['enqueued']}.")
