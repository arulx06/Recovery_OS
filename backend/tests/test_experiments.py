import pytest

from app.core.database import SessionLocal
from app.models import ExperimentCase
from app.services import experiment_runner
from app.ml import train as train_module, scorer


@pytest.fixture(scope="module", autouse=True)
def trained_model():
    """These tests need a real trained model for ml_policy.decide_ml to
    score against — train a small one for speed, restore whatever was
    there afterward (same pattern as test_ml_policy.py)."""
    import shutil
    from pathlib import Path
    backup_dir = Path(train_module.MODEL_PATH).parent / "_test_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "model.joblib"
    had_existing = train_module.MODEL_PATH.exists()
    if had_existing:
        shutil.copy(train_module.MODEL_PATH, backup_path)

    train_module.train(n=6000, seed=42, save=True)
    scorer.reset_cache()

    yield

    if had_existing:
        shutil.copy(backup_path, train_module.MODEL_PATH)
    else:
        train_module.MODEL_PATH.unlink(missing_ok=True)
    scorer.reset_cache()
    backup_path.unlink(missing_ok=True)
    backup_dir.rmdir()


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---- experiment_runner service ----

def test_run_experiment_creates_two_rows_per_scenario(db_session):
    run_id = experiment_runner.run_experiment(db_session, count=10, seed=1)

    rows = db_session.query(ExperimentCase).filter(ExperimentCase.run_id == run_id).all()
    assert len(rows) == 20  # 10 scenarios x 2 arms
    assert {r.arm for r in rows} == {"baseline", "adaptive"}


def test_run_experiment_is_reproducible_with_same_seed(db_session):
    run_id_a = experiment_runner.run_experiment(db_session, count=20, seed=99)
    run_id_b = experiment_runner.run_experiment(db_session, count=20, seed=99)

    summary_a = experiment_runner.summarize_run(db_session, run_id_a)
    summary_b = experiment_runner.summarize_run(db_session, run_id_b)

    assert summary_a["arms"]["baseline"]["amount_recovered"] == summary_b["arms"]["baseline"]["amount_recovered"]
    assert summary_a["arms"]["adaptive"]["amount_recovered"] == summary_b["arms"]["adaptive"]["amount_recovered"]
    assert summary_a["incremental_recovered"] == summary_b["incremental_recovered"]


def test_different_seeds_can_differ(db_session):
    run_id_a = experiment_runner.run_experiment(db_session, count=30, seed=1)
    run_id_b = experiment_runner.run_experiment(db_session, count=30, seed=2)

    summary_a = experiment_runner.summarize_run(db_session, run_id_a)
    summary_b = experiment_runner.summarize_run(db_session, run_id_b)

    # Not a strict guarantee for every possible pair of seeds, but with 30
    # scenarios the amounts drawn should differ between two different seeds.
    assert summary_a["arms"]["baseline"]["amount_at_risk"] != summary_b["arms"]["baseline"]["amount_at_risk"]


def test_summarize_missing_run_returns_not_found(db_session):
    summary = experiment_runner.summarize_run(db_session, "does-not-exist")
    assert summary["found"] is False


def test_summary_shape(db_session):
    run_id = experiment_runner.run_experiment(db_session, count=15, seed=5)
    summary = experiment_runner.summarize_run(db_session, run_id)

    assert summary["case_count"] == 30
    for arm in ("baseline", "adaptive"):
        arm_summary = summary["arms"][arm]
        assert arm_summary["cases"] == 15
        assert arm_summary["amount_at_risk"] > 0
        assert 0.0 <= arm_summary["recovery_rate"] <= 1.0
        assert isinstance(arm_summary["action_distribution"], dict)
        assert sum(arm_summary["action_distribution"].values()) == 15

    assert summary["incremental_recovered"] == (
        summary["arms"]["adaptive"]["amount_recovered"] - summary["arms"]["baseline"]["amount_recovered"]
    )


# ---- API ----

def test_post_experiments_runs_and_returns_summary(client):
    resp = client.post("/experiments", json={"count": 12, "seed": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["case_count"] == 24


def test_post_experiments_rejects_count_out_of_range(client):
    resp = client.post("/experiments", json={"count": 0})
    assert resp.status_code == 400

    resp2 = client.post("/experiments", json={"count": 100000})
    assert resp2.status_code == 400


def test_get_experiment_by_run_id(client):
    run_id = client.post("/experiments", json={"count": 8, "seed": 4}).json()["run_id"]

    resp = client.get(f"/experiments/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["run_id"] == run_id


def test_get_missing_experiment_404s(client):
    resp = client.get("/experiments/does-not-exist")
    assert resp.status_code == 404


def test_list_experiments_includes_recent_run(client):
    run_id = client.post("/experiments", json={"count": 5, "seed": 6}).json()["run_id"]

    resp = client.get("/experiments")
    assert resp.status_code == 200
    run_ids = [r["run_id"] for r in resp.json()]
    assert run_id in run_ids


def test_export_csv_has_one_row_per_case(client):
    run_id = client.post("/experiments", json={"count": 6, "seed": 7}).json()["run_id"]

    resp = client.get(f"/experiments/{run_id}/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    lines = resp.text.strip().splitlines()
    assert len(lines) == 1 + 12  # header + 6 scenarios x 2 arms
    assert lines[0].startswith("run_id,arm,revenue_case_id")


def test_export_csv_missing_run_404s(client):
    resp = client.get("/experiments/does-not-exist/export.csv")
    assert resp.status_code == 404
