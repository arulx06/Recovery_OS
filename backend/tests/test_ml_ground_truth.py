import pytest

from app.ml import ground_truth
from app.ml.synthetic_history import generate_history
from app.ml.costs import ACTION_COST
from app.services.failure_diagnosis import ALL_CATEGORIES
from app.services.policy_engine import ALL_ACTIONS


@pytest.mark.parametrize("category", sorted(ALL_CATEGORIES))
@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_true_probability_is_a_valid_probability(category, action):
    p = ground_truth.true_probability(category, action, amount=1000)
    assert 0.0 <= p <= 0.95


def test_stop_always_has_zero_probability():
    for category in ALL_CATEGORIES:
        assert ground_truth.true_probability(category, "STOP", amount=5000) == 0.0


def test_every_policy_action_has_an_explicit_cost():
    assert set(ACTION_COST) == set(ALL_ACTIONS)


def test_transient_infra_wait_beats_permanent_hard_failure_wait():
    transient = ground_truth.true_probability("TRANSIENT_INFRASTRUCTURE", "WAIT", amount=1000)
    permanent = ground_truth.true_probability("PERMANENT_HARD_FAILURE", "WAIT", amount=1000)
    assert transient > permanent


def test_contact_fatigue_reduces_probability_monotonically():
    p0 = ground_truth.true_probability("CUSTOMER_AUTHENTICATION", "CONTACT_CUSTOMER", amount=1000, previous_contacts=0)
    p1 = ground_truth.true_probability("CUSTOMER_AUTHENTICATION", "CONTACT_CUSTOMER", amount=1000, previous_contacts=1)
    p3 = ground_truth.true_probability("CUSTOMER_AUTHENTICATION", "CONTACT_CUSTOMER", amount=1000, previous_contacts=3)
    assert p0 > p1 > p3


def test_days_overdue_reduces_wait_probability_but_not_contact():
    wait_fresh = ground_truth.true_probability("TRANSIENT_INFRASTRUCTURE", "WAIT", amount=1000, days_overdue=0)
    wait_stale = ground_truth.true_probability("TRANSIENT_INFRASTRUCTURE", "WAIT", amount=1000, days_overdue=20)
    assert wait_fresh > wait_stale

    contact_fresh = ground_truth.true_probability("TRANSIENT_INFRASTRUCTURE", "CONTACT_CUSTOMER", amount=1000, days_overdue=0)
    contact_stale = ground_truth.true_probability("TRANSIENT_INFRASTRUCTURE", "CONTACT_CUSTOMER", amount=1000, days_overdue=20)
    assert contact_fresh == contact_stale  # only WAIT-type actions decay with elapsed time


def test_subscription_linked_favors_native_retry():
    native_retry = ground_truth.true_probability(
        "SUBSCRIPTION_PENDING_NATIVE_RETRY", "WAIT_FOR_NATIVE_RETRY", amount=1000, subscription_linked=True,
    )
    other_action = ground_truth.true_probability(
        "SUBSCRIPTION_PENDING_NATIVE_RETRY", "CONTACT_CUSTOMER", amount=1000, subscription_linked=True,
    )
    assert native_retry > other_action


def test_sample_outcome_is_deterministic_with_seeded_rng():
    import random
    rng1 = random.Random(123)
    rng2 = random.Random(123)
    results1 = [ground_truth.sample_outcome("INVALID_INSTRUMENT", "CREATE_PAYMENT_LINK", 1000, rng=rng1) for _ in range(20)]
    results2 = [ground_truth.sample_outcome("INVALID_INSTRUMENT", "CREATE_PAYMENT_LINK", 1000, rng=rng2) for _ in range(20)]
    assert results1 == results2


# ---- synthetic_history.py ----

def test_generate_history_shape_and_columns():
    df = generate_history(n=200, seed=1)
    assert len(df) == 200
    expected_columns = {
        "case_id", "failure_category", "action_taken", "amount", "days_overdue",
        "previous_contacts", "subscription_linked", "hour", "day_of_week", "recovered",
    }
    assert expected_columns <= set(df.columns)


def test_generate_history_is_deterministic_with_seed():
    df1 = generate_history(n=200, seed=7)
    df2 = generate_history(n=200, seed=7)
    assert df1.equals(df2)


def test_generate_history_different_seeds_differ():
    df1 = generate_history(n=200, seed=1)
    df2 = generate_history(n=200, seed=2)
    assert not df1["recovered"].equals(df2["recovered"])


def test_generate_history_covers_every_category_and_action():
    df = generate_history(n=2000, seed=1)
    assert set(df["failure_category"].unique()) == set(ALL_CATEGORIES)
    assert set(df["action_taken"].unique()) == set(ALL_ACTIONS)


def test_generate_history_recovered_is_boolean():
    df = generate_history(n=100, seed=1)
    assert df["recovered"].isin([True, False]).all()
