import os

import pytest

from app.ml import scorer, train as train_module


def test_predict_recovery_probability_is_in_range(trained_model_path):
    p = scorer.predict_recovery_probability(
        category="TRANSIENT_INFRASTRUCTURE", action="WAIT", amount=1000, model_path=trained_model_path,
    )
    assert 0.0 <= p <= 1.0


def test_stop_short_circuits_without_calling_the_model(tmp_path):
    # Deliberately pass a bogus model_path — if STOP tried to use the
    # model, this would raise ModelNotTrainedError.
    p = scorer.predict_recovery_probability(
        category="UNKNOWN", action="STOP", amount=1000, model_path=tmp_path / "missing" / "model.joblib",
    )
    assert p == 0.0


def test_model_not_trained_raises_clear_error(tmp_path):
    scorer.reset_cache()
    with pytest.raises(scorer.ModelNotTrainedError):
        scorer.predict_recovery_probability(
            category="TRANSIENT_INFRASTRUCTURE", action="WAIT", amount=1000,
            model_path=tmp_path / "missing" / "model.joblib",
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


def test_batched_ranking_matches_one_action_scoring(trained_model_path):
    context = {
        "category": "CUSTOMER_AUTHENTICATION", "amount": 8000,
        "days_overdue": 2, "previous_contacts": 1,
        "subscription_linked": False, "hour": 17, "day_of_week": 4,
    }
    actions = ["WAIT", "CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "ESCALATE", "STOP"]
    expected = sorted(
        [scorer.score_action(context, action, model_path=trained_model_path) for action in actions],
        key=lambda item: item["expected_value"], reverse=True,
    )
    actual = scorer.rank_actions(context, actions, model_path=trained_model_path)

    assert [item["action"] for item in actual] == [item["action"] for item in expected]
    for batched, individual in zip(actual, expected):
        assert batched["p_recovery"] == pytest.approx(individual["p_recovery"])
        assert batched["expected_value"] == pytest.approx(individual["expected_value"])


def test_rank_actions_calls_model_once_for_candidate_set(trained_model_path, monkeypatch):
    bundle = scorer._load(trained_model_path)
    original = bundle["pipeline"].predict_proba
    call_count = 0

    def counted(rows):
        nonlocal call_count
        call_count += 1
        return original(rows)

    monkeypatch.setattr(bundle["pipeline"], "predict_proba", counted)
    scorer.rank_actions(
        {"category": "INVALID_INSTRUMENT", "amount": 5000},
        ["WAIT", "CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "ESCALATE", "STOP"],
        model_path=trained_model_path,
    )
    assert call_count == 1


def test_train_creates_missing_artifact_parents(tmp_path):
    model_path = tmp_path / "fresh" / "clone" / "artifacts" / "model.joblib"
    train_module.train(n=1000, seed=42, save=True, save_path=model_path)
    assert model_path.is_file()


def test_corrupt_model_raises_domain_error(tmp_path):
    model_path = tmp_path / "corrupt.joblib"
    model_path.write_text("not a joblib artifact", encoding="ascii")
    scorer.reset_cache()
    with pytest.raises(scorer.ModelNotTrainedError):
        scorer._load(model_path)


def test_model_cache_reloads_replaced_artifact(tmp_path, monkeypatch):
    model_path = tmp_path / "model.joblib"
    model_path.write_text("first", encoding="ascii")
    original_mtime = model_path.stat().st_mtime_ns
    load_count = 0

    def fake_load(path):
        nonlocal load_count
        load_count += 1
        return {"pipeline": object(), "load_number": load_count}

    monkeypatch.setattr(scorer.joblib, "load", fake_load)
    scorer.reset_cache()
    first = scorer._load(model_path)
    assert scorer._load(model_path) is first

    model_path.write_text("second artifact", encoding="ascii")
    os.utime(model_path, ns=(original_mtime + 1_000_000, original_mtime + 1_000_000))
    second = scorer._load(model_path)

    assert second["load_number"] == 2
