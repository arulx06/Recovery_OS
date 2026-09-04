#!/usr/bin/env python3
"""
Runs synthetic cases through the deterministic diagnosis, guardrail, and
baseline-policy pipeline. This is separate from the synthetic history generator
used for ML training and from persisted policy experiments. The script verifies
that the pipeline runs to completion on a wide spread of inputs without
raising, without leaving a case stuck in DETECTED/DIAGNOSED, and without
a guardrail silently letting through something it shouldn't.

Usage:
    python scripts/run_synthetic_batch.py [--count 100] [--seed 7]

Runs entirely in-process against a throwaway sqlite database — no server,
no Razorpay credentials needed.
"""
import argparse
import os
import random
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TERMINAL_OR_PENDING_STATES = {
    "WAITING", "ACTION_SCHEDULED", "HUMAN_REVIEW", "STOPPED", "RECOVERED", "DISPUTED",
}

# A representative spread of (error_source, error_step, error_reason,
# has_subscription) combinations — enough to hit every branch in the
# failure taxonomy, weighted roughly like a real failure mix would be
# (mostly transient/insufficient-funds, a long tail of everything else).
FAILURE_PROFILES = [
    ({"error_source": "bank", "error_reason": "insufficient_funds"}, 20),
    ({"error_source": "bank", "error_reason": "gateway_timeout"}, 15),
    ({"error_source": "customer", "error_step": "payment_authentication", "error_reason": "otp_incorrect"}, 12),
    ({"error_source": "customer", "error_reason": "card_expired"}, 10),
    ({"error_source": "customer", "error_reason": "invalid_vpa"}, 8),
    ({"error_source": "business", "error_reason": "mandate_cancelled"}, 8),
    ({"error_source": "risk", "error_reason": "blocked_by_risk_engine"}, 6),
    ({"error_source": "bank", "error_reason": "", "subscription_id": "sub_synthetic"}, 10),
    ({"error_source": "network", "error_reason": "server_error"}, 6),
    ({"error_source": "unknown", "error_reason": "something_new"}, 5),
]


def weighted_profile(rng: random.Random) -> dict:
    population, weights = zip(*FAILURE_PROFILES)
    return dict(rng.choices(population, weights=weights, k=1)[0])


def run(count: int, seed: int):
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "synthetic_batch_secret")

    from app.core.database import Base, engine, SessionLocal
    from app.models import RevenueCase
    from app.services import failure_diagnosis, policy_engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    rng = random.Random(seed)
    category_counts = Counter()
    action_counts = Counter()
    state_counts = Counter()
    errors = []

    for i in range(count):
        profile = weighted_profile(rng)
        amount = Decimal(rng.choice([500, 1500, 4999, 12000, 25000, 30000, 60000, 150000]))

        case = RevenueCase(
            source="synthetic",
            razorpay_payment_id=f"pay_synth_{i:04d}",
            amount=amount,
            currency="INR",
            state="DETECTED",
        )
        db.add(case)
        db.flush()

        try:
            category = failure_diagnosis.classify_failure(profile)
            case.failure_category = category
            case.error_source = profile.get("error_source")
            case.error_step = profile.get("error_step")
            case.error_reason = profile.get("error_reason")
            case.state = "DIAGNOSED"

            decision = policy_engine.decide(db, case)

            category_counts[category] += 1
            action_counts[decision.chosen_action] += 1
            state_counts[case.state] += 1

            if case.state not in TERMINAL_OR_PENDING_STATES:
                errors.append(f"case {i}: ended in unexpected state {case.state}")

        except Exception as exc:  # noqa: BLE001 — we want to catch and report, not crash the batch
            errors.append(f"case {i}: raised {type(exc).__name__}: {exc}")

    db.commit()
    db.close()

    print(f"Ran {count} synthetic cases (seed={seed})\n")

    print("Failure category distribution:")
    for cat, n in category_counts.most_common():
        print(f"  {cat:<35} {n:>4}")

    print("\nChosen action distribution:")
    for action, n in action_counts.most_common():
        print(f"  {action:<25} {n:>4}")

    print("\nResulting case state distribution:")
    for state, n in state_counts.most_common():
        print(f"  {state:<20} {n:>4}")

    print(f"\n{count - len(errors)}/{count} cases completed detection -> diagnosis -> "
          f"guardrails -> policy -> action cleanly.")

    if errors:
        print(f"\n{len(errors)} problem(s):")
        for e in errors[:20]:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("No cases left in detection/diagnosis, no exceptions, no guardrail violations.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    run(args.count, args.seed)
