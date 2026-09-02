#!/usr/bin/env python3
"""
Phase 5 exit criteria: "model ranks allowed actions; offline evaluation
reproducible from repo."

Generates N synthetic-but-realistic scenarios. For each one, runs BOTH the
baseline policy (Phase 3's fixed ranked-preference engine) and the ML
policy (Phase 5's expected-value scorer) against an *identical* copy of
the case — then samples an outcome for whichever action each one chose,
using the same ground-truth simulator the model was trained against.

This is a SYNTHETIC SIMULATION BENCHMARK, not a claim about real Razorpay
recovery lift. The model can only ever look as good as the ground-truth
assumptions it was trained on — see app/ml/ground_truth.py for exactly
what those assumptions are. Once RecoveryOS has live outcome data, this
script's simulated outcomes get replaced with real ones; nothing else
about the comparison changes.

Uses common random numbers: when both policies choose the same action for
a given scenario, they get the identical sampled outcome rather than two
independent draws. Without this, comparing two arms that agree on most
actions produces a "difference" that's mostly sampling noise — only
scenarios where the policies actually disagree on the action should be
able to move the comparison.

Usage:
    python scripts/evaluate_policies.py [--count 500] [--seed 11]

Runs entirely in-process against a throwaway sqlite database — no server,
no live Razorpay credentials needed. Requires a trained model
(`python -m app.ml.train` first, if you haven't already).
"""
import argparse
import os
import random
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same realistic failure-type mix as scripts/run_synthetic_batch.py, so the
# two Phase 3 / Phase 5 exit-criteria scripts are evaluating a comparable
# population of cases.
SCENARIO_PROFILES = [
    ({"failure_category": "INSUFFICIENT_BALANCE"}, 22),
    ({"failure_category": "TRANSIENT_INFRASTRUCTURE"}, 16),
    ({"failure_category": "CUSTOMER_AUTHENTICATION"}, 14),
    ({"failure_category": "INVALID_INSTRUMENT"}, 12),
    ({"failure_category": "SUBSCRIPTION_PENDING_NATIVE_RETRY", "subscription_id": "sub_eval"}, 10),
    ({"failure_category": "MANDATE_ISSUE"}, 9),
    ({"failure_category": "PERMANENT_HARD_FAILURE"}, 9),
    ({"failure_category": "UNKNOWN"}, 8),
]

AMOUNTS = [500, 1500, 4999, 9000, 15000, 25000, 40000, 75000]


def weighted_profile(rng: random.Random) -> dict:
    population, weights = zip(*SCENARIO_PROFILES)
    return dict(rng.choices(population, weights=weights, k=1)[0])


def run(count: int, seed: int):
    from app.core.database import Base, engine, SessionLocal
    from app.models import RevenueCase
    from app.services import policy_engine, ml_policy
    from app.ml import ground_truth, scorer
    from app.ml.costs import ACTION_COST

    try:
        scorer._load()  # fail fast with a clear message if untrained
    except scorer.ModelNotTrainedError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    rng = random.Random(seed)

    results = {"baseline": Counter(), "ml": Counter()}
    revenue = {"baseline": {"at_risk": 0.0, "recovered": 0.0}, "ml": {"at_risk": 0.0, "recovered": 0.0}}
    contacts = {"baseline": 0, "ml": 0}
    escalations = {"baseline": 0, "ml": 0}
    action_costs = {"baseline": 0.0, "ml": 0.0}
    evaluation_time = datetime(2026, 1, 7, 12, 0, 0)

    CONTACT_ACTIONS = policy_engine.CONTACT_ACTIONS

    for i in range(count):
        profile = weighted_profile(rng)
        category = profile["failure_category"]
        subscription_linked = "subscription_id" in profile
        amount = Decimal(rng.choice(AMOUNTS))

        # Common random numbers: if both policies choose the SAME action for
        # this scenario, they must get the SAME sampled outcome — it's the
        # same underlying case. Without this, two arms picking an identical
        # action can still show a "difference" that's pure sampling noise,
        # not a real policy disagreement. Only cases where the policies
        # actually chose different actions should be able to diverge.
        outcome_cache: dict[str, bool] = {}
        chosen_actions: dict[str, str] = {}

        for arm, decide_fn in (("baseline", policy_engine.decide), ("ml", ml_policy.decide_ml)):
            case = RevenueCase(
                source="synthetic_eval",
                razorpay_payment_id=f"pay_eval_{arm}_{i:04d}",
                razorpay_subscription_id=profile.get("subscription_id"),
                amount=amount,
                currency="INR",
                failure_category=category,
                state="DIAGNOSED",
            )
            db.add(case)
            db.flush()

            decision = decide_fn(db, case, now=evaluation_time)
            action = decision.chosen_action
            chosen_actions[arm] = action

            if action not in outcome_cache:
                outcome_cache[action] = ground_truth.sample_outcome(
                    category, action, float(amount),
                    days_overdue=0, previous_contacts=0,
                    subscription_linked=subscription_linked, rng=rng,
                )
            recovered = outcome_cache[action]

            revenue[arm]["at_risk"] += float(amount)
            if recovered:
                revenue[arm]["recovered"] += float(amount)
            results[arm][action] += 1
            action_costs[arm] += ACTION_COST[action]
            if action in CONTACT_ACTIONS:
                contacts[arm] += 1
            if action == "ESCALATE":
                escalations[arm] += 1

    db.commit()
    db.close()
    engine.dispose()

    print(f"Ran {count} matched scenarios per policy (seed={seed})")
    print("SYNTHETIC SIMULATION BENCHMARK - not production Razorpay lift.\n")

    print(f"{'Metric':<28}{'Baseline':>14}{'ML / Adaptive':>16}")
    print("-" * 58)

    at_risk_b = revenue["baseline"]["at_risk"]
    at_risk_m = revenue["ml"]["at_risk"]
    rec_b = revenue["baseline"]["recovered"]
    rec_m = revenue["ml"]["recovered"]

    def row(label, b, m, fmt="{:,.0f}"):
        print(f"{label:<28}{fmt.format(b):>14}{fmt.format(m):>16}")

    row("Revenue at risk (INR)", at_risk_b, at_risk_m)
    row("Revenue recovered (INR)", rec_b, rec_m)
    row("Recovery rate", rec_b / at_risk_b * 100 if at_risk_b else 0, rec_m / at_risk_m * 100 if at_risk_m else 0, fmt="{:.1f}%")
    row("Contact actions selected", contacts["baseline"], contacts["ml"], fmt="{:d}")
    row("Escalations", escalations["baseline"], escalations["ml"], fmt="{:d}")
    row("Action cost proxy (INR)", action_costs["baseline"], action_costs["ml"])
    row("Realized net value (INR)", rec_b - action_costs["baseline"], rec_m - action_costs["ml"])

    incremental = rec_m - rec_b
    print("-" * 58)
    sign = "+" if incremental >= 0 else ""
    print(f"{'Incremental recovered (INR)':<28}{'':<14}{sign}{incremental:,.0f}")

    print("\nAction distribution:")
    all_actions = sorted(set(results['baseline']) | set(results['ml']))
    print(f"{'Action':<25}{'Baseline':>14}{'ML':>10}")
    for action in all_actions:
        print(f"{action:<25}{results['baseline'][action]:>14}{results['ml'][action]:>10}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    run(args.count, args.seed)
