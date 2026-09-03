from decimal import Decimal
import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import ExperimentRun
from app.services import experiment_runner
from app.ml import scorer


def test_profile_immutability(db_session=None):
    # Use the autouse db fixture from test_adaptive_policy? We'll use SessionLocal directly
    from app.core.database import SessionLocal as SL
    db = SL()
    try:
        old_profile = settings.ADAPTIVE_POLICY_PROFILE
        old_weight = settings.ADAPTIVE_FRICTION_WEIGHT
        settings.ADAPTIVE_POLICY_PROFILE = "revenue_first"
        settings.ADAPTIVE_FRICTION_WEIGHT = None
        run_id = experiment_runner.run_experiment(db, count=20, seed=123)
        # Change settings after run
        settings.ADAPTIVE_POLICY_PROFILE = "low_friction"
        settings.ADAPTIVE_FRICTION_WEIGHT = None
        # Summarize old run — should still be revenue_first, weight 4
        summary = experiment_runner.summarize_run(db, run_id)
        assert summary["evaluation_friction_profile"] == "revenue_first"
        assert float(summary["evaluation_friction_weight"]) == 4.0
        db.rollback()
    finally:
        settings.ADAPTIVE_POLICY_PROFILE = old_profile
        settings.ADAPTIVE_FRICTION_WEIGHT = old_weight
        db.close()


def test_fingerprint_immutability(monkeypatch):
    from app.core.database import SessionLocal as SL
    db = SL()
    try:
        # First run with real model
        run_id = experiment_runner.run_experiment(db, count=20, seed=456)
        summary1 = experiment_runner.summarize_run(db, run_id)
        fp1 = summary1["model_fingerprint"]
        assert fp1 is not None
        # Monkeypatch model info to different fingerprint
        orig_info = scorer.get_model_info()
        def fake_info(*a, **k):
            d = orig_info.copy()
            d["fingerprint"] = "deadbeef"
            d["fingerprint_short"] = "deadbeef"
            return d
        monkeypatch.setattr(scorer, "get_model_info", fake_info)
        summary2 = experiment_runner.summarize_run(db, run_id)
        assert summary2["model_fingerprint"] == fp1
        assert summary2["model_fingerprint"] != "deadbeef"
        db.rollback()
    finally:
        db.close()


def test_historical_utility_stability():
    from app.core.database import SessionLocal as SL
    db = SL()
    try:
        old_profile = settings.ADAPTIVE_POLICY_PROFILE
        settings.ADAPTIVE_POLICY_PROFILE = "balanced"
        run_id = experiment_runner.run_experiment(db, count=30, seed=789)
        summary1 = experiment_runner.summarize_run(db, run_id)
        util1 = summary1["arms"]["adaptive"]["realized_policy_utility"]
        # Change profile to low_friction — old run's utility must not change (uses persisted weight 18)
        settings.ADAPTIVE_POLICY_PROFILE = "low_friction"
        summary2 = experiment_runner.summarize_run(db, run_id)
        util2 = summary2["arms"]["adaptive"]["realized_policy_utility"]
        assert util1 == util2
        assert summary2["evaluation_friction_profile"] == "balanced"
        db.rollback()
    finally:
        settings.ADAPTIVE_POLICY_PROFILE = old_profile
        db.close()


def test_seed_persistence():
    from app.core.database import SessionLocal as SL
    db = SL()
    try:
        # seed=None should still persist a resolved seed
        run_id = experiment_runner.run_experiment(db, count=10, seed=None)
        run = db.query(ExperimentRun).filter(ExperimentRun.run_id == run_id).first()
        assert run is not None
        assert isinstance(run.resolved_seed, int)
        # Re-run with that seed should be reproducible
        seed = run.resolved_seed
        db2 = SL()
        try:
            run_id2 = experiment_runner.run_experiment(db2, count=10, seed=seed)
            summary1 = experiment_runner.summarize_run(db, run_id)
            summary2 = experiment_runner.summarize_run(db2, run_id2)
            # Baseline should be identical for same seed
            assert summary1["arms"]["baseline"]["amount_at_risk"] == summary2["arms"]["baseline"]["amount_at_risk"]
            assert summary1["arms"]["baseline"]["action_distribution"] == summary2["arms"]["baseline"]["action_distribution"]
        finally:
            db2.close()
        db.rollback()
    finally:
        db.close()


def test_new_session_retrieval():
    from app.core.database import SessionLocal as SL
    db = SL()
    try:
        run_id = experiment_runner.run_experiment(db, count=15, seed=555)
        db.commit()
        db.close()
        # New session
        db2 = SL()
        try:
            summary = experiment_runner.summarize_run(db2, run_id)
            assert summary["found"] is True
            assert summary["model_fingerprint"] is not None
            assert summary["evaluation_friction_profile"] is not None
        finally:
            db2.close()
    finally:
        # db already closed
        pass
