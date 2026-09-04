"""
Persisted synthetic policy experiment runner.

Same matched-scenario, common-random-numbers design as
scripts/evaluate_policies.py (see that file for the methodology writeup),
but persisted: every case in every run gets an ExperimentCase row grouped
by run_id, so the dashboard can show recent runs and provide a case-level CSV
for the audit trail.

Deliberately reuses policy_engine.decide / ml_policy.decide_ml exactly as
they are — this module doesn't reimplement the recovery logic, only adds
persistence and aggregation around calling it.
"""
import hashlib
import random
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import RevenueCase, ExperimentCase, ExperimentRun
from app.services import policy_engine, ml_policy
from app.ml import ground_truth
from app.ml import scorer
from app.ml.costs import ACTION_COST
from app.ml.friction import BASE_FRICTION, get_friction_weight
from app.ml.features import FEATURE_SCHEMA_VERSION
from app.core.config import settings as _settings


def _deterministic_uniform(seed: int, scenario_idx: int, action: str) -> float:
    """Deterministic uniform in [0,1) for outcome — independent of policy path.

    Uses stable hashlib, not Python's randomized hash(), so results are
    reproducible across processes and runs.
    """
    h = hashlib.sha256(f"{seed}:{scenario_idx}:{action}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF

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

    If seed is None, a random seed is generated and persisted as
    ExperimentRun.resolved_seed so the scenario set can be reproduced.
    """
    # Persist an automatically generated seed so every run is reproducible.
    seed = seed if seed is not None else random.randrange(2**31)
    # Capture provenance at run time — historical runs must be immutable
    model_info = scorer.get_model_info()
    if not model_info.get("available"):
        raise scorer.ModelNotTrainedError(model_info.get("reason") or "model unavailable")
    profile, weight = get_friction_weight(_settings.ADAPTIVE_POLICY_PROFILE, _settings.ADAPTIVE_FRICTION_WEIGHT)
    run_id = str(uuid.uuid4())
    # Persist run-level provenance before scenarios
    db.add(ExperimentRun(
        run_id=run_id,
        resolved_seed=int(seed),
        scenario_count=int(count),
        model_version=model_info.get("model_version"),
        model_fingerprint=model_info.get("fingerprint_short") or model_info.get("fingerprint"),
        feature_schema_version=model_info.get("feature_schema_version") or FEATURE_SCHEMA_VERSION,
        adaptive_profile=profile,
        friction_weight=weight,
    ))
    db.flush()
    scenario_rng = random.Random(seed)
    evaluation_time = datetime(2026, 1, 7, 12, 0, 0)

    CONTACT_ACTIONS = policy_engine.CONTACT_ACTIONS

    for i in range(count):
        profile = _weighted_profile(scenario_rng)
        category = profile["failure_category"]
        subscription_linked = "subscription_id" in profile
        amount = Decimal(scenario_rng.choice(AMOUNTS))

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

            decision = decide_fn(db, case, now=evaluation_time)
            action = decision.chosen_action

            if action not in outcome_cache:
                # Deterministic outcome — same (seed, scenario_idx, action) always gives same uniform,
                # so later scenarios are not perturbed by earlier policy choices.
                p = ground_truth.true_probability(
                    category, action, float(amount),
                    days_overdue=0, previous_contacts=0,
                    subscription_linked=subscription_linked,
                )
                uniform = _deterministic_uniform(seed, i, action)
                outcome_cache[action] = uniform < p
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
    distribution per arm. Also exposes evaluation provenance.
    """
    rows = db.query(ExperimentCase).filter(ExperimentCase.run_id == run_id).all()
    if not rows:
        return {"run_id": run_id, "found": False}

    # Historical provenance — read from ExperimentRun persisted at run time
    run_meta = db.query(ExperimentRun).filter(ExperimentRun.run_id == run_id).first()
    if run_meta is not None:
        eval_weight = float(run_meta.friction_weight) if run_meta.friction_weight is not None else 18.0
        eval_profile = run_meta.adaptive_profile
        model_version = run_meta.model_version
        model_fingerprint = run_meta.model_fingerprint
        feature_schema_version = run_meta.feature_schema_version
        resolved_seed = run_meta.resolved_seed
        scenario_count_persisted = run_meta.scenario_count
        model_available = True
        reproducibility_note = "historical provenance is immutable as of run time"
    else:
        # Fallback for pre-migration runs without ExperimentRun row
        try:
            model_info = scorer.get_model_info()
        except Exception:
            model_info = {"available": False}
        from app.core.config import settings as _settings_fallback
        from app.ml.friction import get_friction_weight as _get_weight_fallback
        _, eval_weight = _get_weight_fallback(_settings_fallback.ADAPTIVE_POLICY_PROFILE, _settings_fallback.ADAPTIVE_FRICTION_WEIGHT)
        eval_profile = _settings_fallback.ADAPTIVE_POLICY_PROFILE
        model_version = model_info.get("model_version")
        model_fingerprint = model_info.get("fingerprint_short") or model_info.get("fingerprint")
        feature_schema_version = model_info.get("feature_schema_version")
        resolved_seed = None
        scenario_count_persisted = len(rows) // 2
        model_available = model_info.get("available")
        reproducibility_note = "pre-migration run without persisted ExperimentRun — seed/profile not durable"
    summary = {
        "run_id": run_id,
        "found": True,
        "case_count": len(rows),
        "scenario_count": scenario_count_persisted,
        "created_at": min(r.created_at for r in rows).isoformat() if rows else None,
        "resolved_seed": resolved_seed,
        "evaluation_friction_weight": eval_weight,
        "evaluation_friction_profile": eval_profile,
        "model_version": model_version,
        "model_fingerprint": model_fingerprint,
        "feature_schema_version": feature_schema_version,
        "model_available": model_available,
        "reproducibility_note": reproducibility_note,
        "arms": {},
    }

    for arm in ("baseline", "adaptive"):
        arm_rows = [r for r in rows if r.arm == arm]
        at_risk = sum(float(r.amount_at_risk or 0) for r in arm_rows)
        recovered_amt = sum(float(r.amount_recovered or 0) for r in arm_rows)
        contacts = sum(r.contacts_made or 0 for r in arm_rows)
        escalations = sum(1 for r in arm_rows if r.chosen_action == "ESCALATE")
        action_cost = sum(ACTION_COST[r.chosen_action] for r in arm_rows if r.chosen_action)
        friction = sum(BASE_FRICTION.get(r.chosen_action, 0) for r in arm_rows if r.chosen_action)
        waits = sum(1 for r in arm_rows if r.chosen_action == "WAIT")
        native_waits = sum(1 for r in arm_rows if r.chosen_action == "WAIT_FOR_NATIVE_RETRY")
        payment_links = sum(1 for r in arm_rows if r.chosen_action == "CREATE_PAYMENT_LINK")
        ptps = sum(1 for r in arm_rows if r.chosen_action == "COLLECT_PROMISE_TO_PAY")

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
            "contact_rate": (contacts / len(arm_rows) * 100) if arm_rows else 0.0,
            "contacts_per_100": (contacts / len(arm_rows) * 100) if arm_rows else 0.0,
            "escalations": escalations,
            "waits": waits,
            "native_retry_waits": native_waits,
            "payment_links": payment_links,
            "ptps": ptps,
            "action_cost_proxy": action_cost,
            "friction_score": friction,
            "realized_net_value": recovered_amt - action_cost,
            "realized_policy_utility": recovered_amt - action_cost - (summary["evaluation_friction_weight"] * friction),
            "utility": recovered_amt - action_cost - friction,  # deprecated alias (weight 1.0) — use realized_policy_utility
            "evaluation_friction_weight": summary["evaluation_friction_weight"],
            "recovered_per_contact": (recovered_amt / contacts) if contacts else (recovered_amt if recovered_amt else 0.0),
            "action_distribution": action_dist,
        }

    baseline_rec = summary["arms"].get("baseline", {}).get("amount_recovered", 0.0)
    adaptive_rec = summary["arms"].get("adaptive", {}).get("amount_recovered", 0.0)
    summary["incremental_recovered"] = adaptive_rec - baseline_rec

    return summary
