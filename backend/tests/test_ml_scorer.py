from pathlib import Path

import pytest

from app.ml import scorer, train as train_module


@pytest.fixture(scope="module")
def trained_model_path(tmp_path_factory):
    """Train a small model once per test module into an isolated path —
    tests must never depend on (or clobber) the repo's real artifact."""
    model_dir = tmp_path_factory.mktemp("ml_model")
    model_path = model_dir / "model.joblib"
    train_module.train(n=6000, seed=42, save=True, save_path=model_path)
    scorer.reset_cache()
    yield model_path
    scorer.reset_cache()


def test_predict_recovery_probability_is_in_range(trained_model_path):
    p = scorer.predict_recovery_probability(
        category="TRANSIENT_INFRASTRUCTURE", action="WAIT", amount=1000, model_path=trained_model_path,
    )
    assert 0.0 <= p <= 1.0


def test_stop_short_circuits_without_calling_the_model(trained_model_path):
    # Deliberately pass a bogus model_path — if STOP tried to use the
    # model, this would raise ModelNotTrainedError.
    p = scorer.predict_recovery_probability(
        category="UNKNOWN", action="STOP", amount=1000, model_path=Path("/nonexistent/model.joblib"),
    )
    assert p == 0.0


def test_model_not_trained_raises_clear_error():
    scorer.reset_cache()
    with pytest.raises(scorer.ModelNotTrainedError):
        scorer.predict_recovery_probability(
            category="TRANSIENT_INFRASTRUCTURE", action="WAIT", amount=1000,
            model_path=Path("/nonexistent/model.joblib"),
        )


def test_score_action_computes_expected_value_as_probability_times_amount_minus_cost(trained_model_path):
    context = {"category": "TRANSIENT_INFRASTRUCTURE", "amount": 1000}
    result = scorer.score_action(context, "WAIT", model_path=trained_model_path)
    assert result["cost"] == 0.0  # WAIT is free
    assert result["expected_value"] == pytest.approx(result["p_recovery"] * 1000 - 0.0)


def test_rank_actions_sorts_by_expected_value_descending(trained_model_path):
    context = {"category": "INVALID_INSTRUMENT", "amount": 5000}
    ranked = scorer.rank_actions(context, ["WAIT", "CREATE_PAYMENT_LINK", "ESCALATE"], model_path=trained_model_path)
    evs = [r["expected_value"] for r in ranked]
    assert evs == sorted(evs, reverse=True)


def test_rank_actions_prefers_contact_type_action_for_invalid_instrument(trained_model_path):
    # Ground truth heavily favors CREATE_PAYMENT_LINK for INVALID_INSTRUMENT
    # over WAIT — the trained model should have picked that signal up.
    context = {"category": "INVALID_INSTRUMENT", "amount": 5000}
    ranked = scorer.rank_actions(context, ["WAIT", "CREATE_PAYMENT_LINK"], model_path=trained_model_path)
    assert ranked[0]["action"] == "CREATE_PAYMENT_LINK"


def test_rank_actions_empty_list_returns_empty(trained_model_path):
    assert scorer.rank_actions({"category": "UNKNOWN", "amount": 100}, [], model_path=trained_model_path) == []
