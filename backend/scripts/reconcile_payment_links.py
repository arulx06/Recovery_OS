#!/usr/bin/env python3
"""
Manual provider-side Payment Link reconciliation.

Usage (from backend/):
  python scripts/reconcile_payment_links.py --action-id <uuid>
  python scripts/reconcile_payment_links.py --all-ambiguous
  python scripts/reconcile_payment_links.py --all-ambiguous --dry-run

Never prints credentials. Provider payloads are sanitized.
"""
import argparse
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Action, RevenueCase
from app.services import razorpay_client, temporal_runtime


def _sanitize_link(link: dict) -> dict:
    return {
        "id": link.get("id"),
        "reference_id": link.get("reference_id"),
        "short_url": link.get("short_url"),
        "amount": link.get("amount"),
        "currency": link.get("currency"),
        "status": link.get("status"),
        "notes": link.get("notes"),
    }


def reconcile_one(action_id: str, dry_run: bool = False) -> str:
    with SessionLocal() as db:
        action = db.query(Action).filter(Action.id == action_id).first()
        if not action:
            return f"not found: {action_id}"
        if action.action_type != "CREATE_PAYMENT_LINK":
            return f"skipped: {action_id} is {action.action_type}, not CREATE_PAYMENT_LINK"
        case = db.query(RevenueCase).filter(RevenueCase.id == action.revenue_case_id).first()
        if not case:
            return f"case missing for {action_id}"
        snapshot = (action.id, action.action_type, case)

    if not settings.RAZORPAY_API_ENABLED:
        return f"simulation mode — provider reconciliation not applicable for {action_id} (RAZORPAY_API_ENABLED=false)"

    expected_amount_paise = int((case.amount or Decimal("0")) * 100)
    expected_currency = case.currency or "INR"

    try:
        link = razorpay_client.find_payment_link_for_action(
            action_id, expected_amount_paise, expected_currency, case.id,
        )
    except razorpay_client.RazorpayAmbiguousError as exc:
        return f"reconcile pending for {action_id}: lookup ambiguous ({exc})"
    except razorpay_client.RazorpayAPIError as exc:
        return f"mismatch for {action_id}: {exc} — manual review required"

    if link is None:
        return f"absent for {action_id}: provider has no matching link (safe to retry if within attempts)"

    sanitized = _sanitize_link(link)
    if dry_run:
        return f"would reconcile {action_id} -> {sanitized['id']} ({sanitized['status']}) {sanitized['short_url']}"

    # Attempt to adopt via temporal runtime's stale/ambiguous path.
    # For a SCHEDULED or EXECUTING action, we can directly complete.
    with SessionLocal() as db:
        act = db.query(Action).filter(Action.id == action_id).first()
        if not act:
            return f"disappeared: {action_id}"
        # If EXECUTING, use the reconcile path that respects lease.
        # For simplicity, try to claim if SCHEDULED, then reconcile.
        db.rollback()

    # Use the same provider-adopt logic as the worker: set _reconciled and complete.
    # We reuse the worker's handling by temporarily marking EXECUTING if needed.
    with SessionLocal() as db:
        action = db.query(Action).filter(Action.id == action_id).with_for_update().first()
        case = db.query(RevenueCase).filter(RevenueCase.id == action.revenue_case_id).with_for_update().first()
        if not action or not case:
            db.rollback()
            return f"stale: {action_id}"
        if action.status not in ("SCHEDULED", "EXECUTING"):
            db.rollback()
            return f"skipped: {action_id} status {action.status} not reconcile-able"
        if case.state not in ("ACTION_SCHEDULED", "WAITING", "AWAITING_OUTCOME"):
            # Allow AWAITING_OUTCOME/WAITING in some edge cases, but ACTION_SCHEDULED is primary.
            pass
        # Ensure we are EXECUTING for the helper.
        was_scheduled = action.status == "SCHEDULED"
        if was_scheduled:
            action.status = "EXECUTING"
            action.claimed_at = temporal_runtime.utc_now()
            action.attempt_count = max(action.attempt_count, 1)
            db.flush()
            db.commit()
        else:
            db.rollback()

    reconciled = dict(link)
    reconciled["_reconciled"] = True
    outcome = temporal_runtime._complete_external(action_id, reconciled, temporal_runtime.utc_now())
    return f"reconciled {action_id} -> {sanitized['id']} ({sanitized['status']}) outcome={outcome}"


def scan_ambiguous(dry_run: bool = False):
    with SessionLocal() as db:
        rows = (
            db.query(Action)
            .filter(
                Action.action_type == "CREATE_PAYMENT_LINK",
                Action.status.in_(("SCHEDULED", "EXECUTING")),
            )
            .all()
        )
        candidates = []
        for a in rows:
            if a.last_error and "ambiguous" in a.last_error.lower():
                candidates.append(a.id)
            elif a.status == "EXECUTING" and a.claimed_at is not None:
                # Stale EXECUTING not yet reclaimed
                candidates.append(a.id)
        db.rollback()
    if not candidates:
        print("No ambiguous Payment Link actions found.")
        return
    print(f"Found {len(candidates)} candidate(s):")
    for aid in candidates:
        print(reconcile_one(aid, dry_run=dry_run))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--action-id", help="single Action.id to reconcile")
    group.add_argument("--all-ambiguous", action="store_true", help="scan all ambiguous payment-link actions")
    parser.add_argument("--dry-run", action="store_true", help="do not mutate DB, only show what would happen")
    args = parser.parse_args()

    if not settings.DATABASE_URL:
        print("DATABASE_URL not configured", file=sys.stderr)
        sys.exit(1)

    if args.action_id:
        print(reconcile_one(args.action_id, dry_run=args.dry_run))
    else:
        scan_ambiguous(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
