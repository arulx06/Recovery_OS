"""
Canonical feature pipeline — single source of truth for train and live inference.

Every live-available feature is derived from authoritative state available at
decision time. No post-action or ground-truth label is used.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RevenueCase
from app.services import policy_engine

CATEGORICAL_FEATURES = ["failure_category", "action_taken"]
NUMERIC_FEATURES = ["amount", "days_overdue", "previous_contacts", "subscription_linked", "hour", "day_of_week"]
FEATURE_COLUMNS = CATEGORICAL_FEATURES + NUMERIC_FEATURES

# Schema version for manifest compatibility
FEATURE_SCHEMA_VERSION = "v1"


def build_live_features(
    db: Session,
    case: RevenueCase,
    action: str,
    now: datetime,
) -> dict:
    """Build one feature row for a (case, candidate action) at decision time.

    All inputs are LIVE_AVAILABLE or DERIVABLE_FROM_HISTORY:
    - failure_category: from RevenueCase.failure_category (LIVE_AVAILABLE)
    - action_taken: candidate action being scored (ACTION-CONDITIONAL)
    - amount: RevenueCase.amount (LIVE_AVAILABLE)
    - days_overdue: (now - case.created_at) in days (DERIVABLE_FROM_HISTORY, bounded query)
    - previous_contacts: count of prior contact actions for case (DERIVABLE_FROM_HISTORY, bounded: contacts_for_case)
    - subscription_linked: bool(case.razorpay_subscription_id) (LIVE_AVAILABLE)
    - hour, day_of_week: from decision-time clock (LIVE_AVAILABLE, deterministic)

    No synthetic-only or target-leakage feature is used.
    """
    previous_contacts = len(policy_engine.contacts_for_case(db, case))
    days_overdue = 0.0
    if case.created_at:
        delta = now - case.created_at
        days_overdue = max(0.0, delta.total_seconds() / 86400.0)

    return {
        "failure_category": case.failure_category or "UNKNOWN",
        "action_taken": action,
        "amount": float(case.amount) if case.amount is not None else 0.0,
        "days_overdue": float(days_overdue),
        "previous_contacts": int(previous_contacts),
        "subscription_linked": bool(case.razorpay_subscription_id),
        "hour": int(now.hour),
        "day_of_week": int(now.weekday()),
    }


def validate_feature_row(row: dict) -> None:
    """Raise if required columns missing or types invalid — prevents silent drift."""
    for col in FEATURE_COLUMNS:
        if col not in row:
            raise ValueError(f"missing feature column: {col}")
    # Basic type checks
    if row["failure_category"] not in {
        "TRANSIENT_INFRASTRUCTURE",
        "SUBSCRIPTION_PENDING_NATIVE_RETRY",
        "INSUFFICIENT_BALANCE",
        "CUSTOMER_AUTHENTICATION",
        "INVALID_INSTRUMENT",
        "MANDATE_ISSUE",
        "PERMANENT_HARD_FAILURE",
        "UNKNOWN",
    }:
        # Allow unknown handling via OneHotEncoder(handle_unknown="ignore")
        pass
    if not isinstance(row["amount"], (int, float)):
        raise ValueError(f"invalid amount type: {type(row['amount'])}")
