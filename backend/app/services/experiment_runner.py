"""
Experiment runner (Phase 7).

Same matched-scenario, common-random-numbers design as
scripts/evaluate_policies.py (see that file for the methodology writeup),
but persisted: every case in every run gets an ExperimentCase row grouped
by run_id, so the dashboard can show "the last run" and a case-level CSV
can be downloaded for the audit trail. This is what
"one-click 500+ case experiment with reproducible metrics and downloadable
audit" (Phase 7's exit criteria) actually runs.

Deliberately reuses policy_engine.decide / ml_policy.decide_ml exactly as
they are — this module doesn't reimplement the recovery logic, only adds
persistence and aggregation around calling it.
"""
import random
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import RevenueCase, ExperimentCase
from app.services import policy_engine, ml_policy
from app.ml import ground_truth

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


def _weighted_profile(rng: random.Random) -> dict:
    population, weights = zip(*SCENARIO_PROFILES)
    return dict(rng.choices(population, weights=weights, k=1)[0])


def run_experiment(db: Session, count: int = 500, seed: int | None = None) -> str:
    """
    Runs `count` matched synthetic scenarios through both policies,
    persists one ExperimentCase per (scenario, arm), and returns the
    run_id. Does not compute the summary — call summarize_run() for that,
    so a huge run can be persisted without holding aggregates in memory
    beyond what SQL aggregation needs.
    """
    seed = seed if seed is not None else random.randrange(2**31)
    run_id = str(uuid.uuid4())
    rng = random.Random(seed)

    CONTACT_ACTIONS = policy_engine.CONTACT_ACTIONS

    for i in range(count):
        profile = _weighted_profile(rng)
        category = profile["failure_category"]
        subscription_linked = "subscription_id" in profile
        amount = Decimal(rng.choice(AMOUNTS))

        outcome_cache: dict[str, bool] = {}

        for arm, decide_fn in (("baseline", policy_engine.decide), ("adaptive", ml_policy.decide_ml)):
            case = RevenueCase(
                source="experiment",
                razorpay_payment_id=f"pay_exp_{run_id[:8]}_{arm}_{i:05d}",
                razorpay_subscription_id=profile.get("subscription_id"),
                amount=amount,
                currency="INR",
                failure_category=category,
                state="DIAGNOSED",
            )
            db.add(case)
            db.flush()

            decision = decide_fn(db, case)
            action = decision.chosen_action

            if action not in outcome_cache:
                outcome_cache[action] = ground_truth.sample_outcome(
                    category, action, float(amount),
                    days_overdue=0, previous_contacts=0,
                    subscription_linked=subscription_linked, rng=rng,
                )
            recovered = outcome_cache[action]

            db.add(ExperimentCase(
                revenue_case_id=case.id,
                run_id=run_id,
                arm=arm,
                failure_category=category,
                chosen_action=action,
                amount_at_risk=amount,
                amount_recovered=amount if recovered else Decimal("0"),
                contacts_made=1 if action in CONTACT_ACTIONS else 0,
                recovered=recovered,
            ))

        if i % 200 == 0:
            db.flush()  # periodic flush so a large run doesn't hold everything in the session at once

    db.commit()
    return run_id


def summarize_run(db: Session, run_id: str) -> dict:
    """
    Aggregates the persisted ExperimentCase rows for one run into the
    metrics the dashboard shows: per-arm revenue at risk/recovered,
    recovery rate, contacts, escalations, incremental ₹, and the action
    distribution per arm.
    """
    rows = db.query(ExperimentCase).filter(ExperimentCase.run_id == run_id).all()
    if not rows:
        return {"run_id": run_id, "found": False}

    summary = {
        "run_id": run_id,
        "found": True,
        "case_count": len(rows),
        "created_at": min(r.created_at for r in rows).isoformat() if rows else None,
        "arms": {},
    }

    for arm in ("baseline", "adaptive"):
        arm_rows = [r for r in rows if r.arm == arm]
        at_risk = sum(float(r.amount_at_risk or 0) for r in arm_rows)
        recovered_amt = sum(float(r.amount_recovered or 0) for r in arm_rows)
        contacts = sum(r.contacts_made or 0 for r in arm_rows)
        escalations = sum(1 for r in arm_rows if r.chosen_action == "ESCALATE")

        action_dist: dict[str, int] = {}
        for r in arm_rows:
            if r.chosen_action:
                action_dist[r.chosen_action] = action_dist.get(r.chosen_action, 0) + 1

        summary["arms"][arm] = {
            "cases": len(arm_rows),
            "amount_at_risk": at_risk,
            "amount_recovered": recovered_amt,
            "recovery_rate": (recovered_amt / at_risk) if at_risk else 0.0,
            "contacts": contacts,
            "escalations": escalations,
            "action_distribution": action_dist,
        }

    baseline_rec = summary["arms"].get("baseline", {}).get("amount_recovered", 0.0)
    adaptive_rec = summary["arms"].get("adaptive", {}).get("amount_recovered", 0.0)
    summary["incremental_recovered"] = adaptive_rec - baseline_rec

    return summary
