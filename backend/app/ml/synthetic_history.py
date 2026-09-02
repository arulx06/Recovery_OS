"""
Synthetic recovery-history generator (Phase 5).

Produces a table of {context, action_taken, recovered} rows that
train.py fits the recovery-probability model on. Every row's outcome comes
from ground_truth.sample_outcome() — so the model is learning to
reconstruct the simulator's assumptions, not real Razorpay behavior. Once
RecoveryOS has run against live traffic, this generator is what gets
replaced with real historical cases; nothing else in Phase 5 needs to
change when that happens, since train.py only cares about getting a
DataFrame with these columns.

`action_taken` is sampled uniformly across the whole action space (not
just the baseline policy's top pick) specifically so the model sees
recovery outcomes for actions the baseline would never have chosen for a
given category — that's what lets it learn "actually, CONTACT_CUSTOMER
works better than WAIT for this category" instead of only ever confirming
the baseline's own preferences.
"""
import random
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./ml_generate.db")

import pandas as pd

from app.services.failure_diagnosis import ALL_CATEGORIES
from app.services.policy_engine import ALL_ACTIONS
from app.ml.ground_truth import sample_outcome

CATEGORIES = sorted(ALL_CATEGORIES)

# Roughly mirrors a realistic failure mix (see scripts/run_synthetic_batch.py's
# FAILURE_PROFILES) — mostly balance/transient/auth issues, a long tail of
# everything else.
CATEGORY_WEIGHTS = {
    "INSUFFICIENT_BALANCE": 22,
    "TRANSIENT_INFRASTRUCTURE": 16,
    "CUSTOMER_AUTHENTICATION": 14,
    "INVALID_INSTRUMENT": 12,
    "SUBSCRIPTION_PENDING_NATIVE_RETRY": 10,
    "MANDATE_ISSUE": 9,
    "PERMANENT_HARD_FAILURE": 9,
    "UNKNOWN": 8,
}


def generate_history(n: int = 3000, seed: int = 42) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []

    categories = list(CATEGORY_WEIGHTS.keys())
    weights = list(CATEGORY_WEIGHTS.values())

    for i in range(n):
        category = rng.choices(categories, weights=weights, k=1)[0]
        action = rng.choice(ALL_ACTIONS)

        amount = rng.choice([500, 1000, 2500, 5000, 9999, 15000, 25000, 40000, 75000, 150000])
        days_overdue = rng.choices([0, 1, 2, 3, 5, 7, 10, 14, 21], weights=[30, 20, 15, 10, 8, 7, 5, 3, 2])[0]
        previous_contacts = rng.choices([0, 1, 2, 3], weights=[60, 25, 10, 5])[0]
        subscription_linked = (category == "SUBSCRIPTION_PENDING_NATIVE_RETRY") or rng.random() < 0.08
        hour = rng.randint(0, 23)
        day_of_week = rng.randint(0, 6)

        recovered = sample_outcome(
            category, action, amount, days_overdue, previous_contacts, subscription_linked, rng=rng,
        )

        rows.append({
            "case_id": f"synth_{i:05d}",
            "failure_category": category,
            "action_taken": action,
            "amount": amount,
            "days_overdue": days_overdue,
            "previous_contacts": previous_contacts,
            "subscription_linked": subscription_linked,
            "hour": hour,
            "day_of_week": day_of_week,
            "recovered": recovered,
        })

    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    df = generate_history(n=n, seed=42)

    out_dir = Path(__file__).resolve().parents[3] / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "synthetic_recovery_history.csv"
    df.to_csv(out_path, index=False)

    print(f"Wrote {len(df)} synthetic rows to {out_path}")
    print(f"Recovery rate overall: {df['recovered'].mean():.1%}")
    print("\nRecovery rate by category:")
    print(df.groupby("failure_category")["recovered"].mean().sort_values(ascending=False).round(3))
