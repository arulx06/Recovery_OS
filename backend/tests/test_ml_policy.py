from decimal import Decimal

import pytest

from app.core.database import SessionLocal
from app.models import RevenueCase, Action
from app.services import ml_policy, policy_engine
from app.ml import scorer


@pytest.fixture(scope="module", autouse=True)
def trained_model(trained_model_path):
    original_path = scorer.MODEL_PATH
    scorer.MODEL_PATH = trained_model_path
    scorer.reset_cache()
    yield
    scorer.reset_cache()
    scorer.MODEL_PATH = original_path


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_case(db, **overrides) -> RevenueCase:
    defaults = dict(
        source="synthetic",
        amount=Decimal("1000"),
        currency="INR",
        failure_category="TRANSIENT_INFRASTRUCTURE",
        state="DIAGNOSED",
    )
    defaults.update(overrides)
    case = RevenueCase(**defaults)
    db.add(case)
    db.flush()
    return case


def test_decide_ml_is_a_noop_outside_diagnosed_state(db_session):
    case = make_case(db_session, state="WAITING")
    assert ml_policy.decide_ml(db_session, case) is None
    assert case.state == "WAITING"


def test_decide_ml_produces_a_decision_with_expected_value_alternatives(db_session):
    case = make_case(db_session, failure_category="INVALID_INSTRUMENT", amount=Decimal("2000"))
    decision = ml_policy.decide_ml(db_session, case)

    assert decision is not None
    assert decision.chosen_action in policy_engine.ALL_ACTIONS
    chosen_alt = decision.alternatives[decision.chosen_action]
    assert "expected_value" in chosen_alt
    assert "p_recovery" in chosen_alt
    assert float(decision.expected_value) == pytest.approx(chosen_alt["expected_value"], abs=0.01)


def test_decide_ml_respects_the_same_amount_guardrail_as_baseline(db_session):
    case = make_case(db_session, failure_category="INVALID_INSTRUMENT", amount=Decimal("99999"))
    decision = ml_policy.decide_ml(db_session, case)
    # Large amount blocks CONTACT_ACTIONS just like the baseline — ML can't
    # spend past a guardrail just because it scores well.
    assert decision.chosen_action not in policy_engine.CONTACT_ACTIONS
    assert case.state != "ACTION_SCHEDULED"


def test_decide_ml_respects_stopping_rule(db_session):
    from app.models import AuditEvent

    case = make_case(db_session, failure_category="TRANSIENT_INFRASTRUCTURE")
    config = policy_engine.GuardrailConfig(max_total_attempts=1)
    for _ in range(3):
        db_session.add(AuditEvent(revenue_case_id=case.id, event="failure_event_received", detail={}))
    db_session.flush()

    decision = ml_policy.decide_ml(db_session, case, config=config)
    assert decision.chosen_action == "STOP"
    assert case.state == "STOPPED"


def test_decide_ml_creates_a_matching_scheduled_action(db_session):
    case = make_case(db_session, failure_category="TRANSIENT_INFRASTRUCTURE")
    decision = ml_policy.decide_ml(db_session, case)

    action = (
        db_session.query(Action)
        .filter(Action.revenue_case_id == case.id, Action.decision_id == decision.id)
        .one()
    )
    assert action.action_type == decision.chosen_action


def test_decide_ml_falls_back_to_escalate_when_model_missing(db_session, monkeypatch):
    def raise_not_trained(*args, **kwargs):
        raise scorer.ModelNotTrainedError("no model for this test")

    monkeypatch.setattr(scorer, "rank_actions_with_friction", raise_not_trained)
    monkeypatch.setattr(scorer, "rank_actions", raise_not_trained, raising=False)

    case = make_case(db_session, failure_category="TRANSIENT_INFRASTRUCTURE")
    # Direct ml_policy call now raises for model failure — dispatcher is the single fallback owner
    with pytest.raises(scorer.ModelNotTrainedError):
        ml_policy.decide_ml(db_session, case)
    assert case.state == "DIAGNOSED"
    # Dispatcher fallback still works
    from app.services import policy_dispatcher
    from app.core.config import settings as _settings
    old = _settings.RECOVERY_POLICY
    _settings.RECOVERY_POLICY = "adaptive"
    try:
        # Need a fresh case because previous was left in DECISION_READY
        case2 = make_case(db_session, failure_category="TRANSIENT_INFRASTRUCTURE")
        decision = policy_dispatcher.decide_for_case(db_session, case2)
        assert decision.policy_mode == "adaptive_fallback"
    finally:
        _settings.RECOVERY_POLICY = old
