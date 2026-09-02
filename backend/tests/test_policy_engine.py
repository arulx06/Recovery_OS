from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.database import SessionLocal
from app.models import RevenueCase, Action
from app.services import policy_engine


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


# ---- baseline category -> action mapping (top-ranked, nothing blocking it) ----

CATEGORY_TOP_CHOICE = [
    ("TRANSIENT_INFRASTRUCTURE", "WAIT"),
    ("SUBSCRIPTION_PENDING_NATIVE_RETRY", "WAIT_FOR_NATIVE_RETRY"),
    ("INSUFFICIENT_BALANCE", "WAIT"),
    ("CUSTOMER_AUTHENTICATION", "CONTACT_CUSTOMER"),
    ("INVALID_INSTRUMENT", "CREATE_PAYMENT_LINK"),
    ("MANDATE_ISSUE", "CONTACT_CUSTOMER"),
    ("PERMANENT_HARD_FAILURE", "ESCALATE"),
    ("UNKNOWN", "ESCALATE"),
]


@pytest.mark.parametrize("category,expected_action", CATEGORY_TOP_CHOICE)
def test_baseline_picks_expected_action_when_unblocked(db_session, category, expected_action):
    case = make_case(db_session, failure_category=category, amount=Decimal("1000"))
    decision = policy_engine.decide(db_session, case)
    assert decision.chosen_action == expected_action
    assert case.state == policy_engine.ACTION_TO_STATE[expected_action]


def test_decide_is_a_noop_outside_diagnosed_state(db_session):
    case = make_case(db_session, state="WAITING")
    assert policy_engine.decide(db_session, case) is None
    assert case.state == "WAITING"  # untouched


# ---- guardrails ----

def test_amount_over_cap_forces_escalation_even_for_contact_actions(db_session):
    case = make_case(
        db_session, failure_category="INVALID_INSTRUMENT", amount=Decimal("50000")
    )
    decision = policy_engine.decide(db_session, case)
    assert decision.chosen_action == "ESCALATE"
    assert case.state == "HUMAN_REVIEW"


def test_wait_is_unaffected_by_amount_cap(db_session):
    # WAIT isn't a spend/contact action, so a large amount shouldn't force
    # escalation for a transient failure — no reason to involve a human
    # just because the payment happens to be large.
    case = make_case(
        db_session, failure_category="TRANSIENT_INFRASTRUCTURE", amount=Decimal("999999")
    )
    decision = policy_engine.decide(db_session, case)
    assert decision.chosen_action == "WAIT"
    assert case.state == "WAITING"


def test_max_contacts_per_case_blocks_further_contact(db_session):
    case = make_case(db_session, failure_category="CUSTOMER_AUTHENTICATION", amount=Decimal("500"))
    config = policy_engine.GuardrailConfig(max_contacts_per_case=2)
    now = datetime.utcnow()

    # Seed 2 prior contact actions directly (simulating history from earlier decisions).
    for i in range(2):
        db_session.add(Action(
            revenue_case_id=case.id, action_type="CONTACT_CUSTOMER",
            status="EXECUTED", created_at=now - timedelta(days=1, hours=i),
        ))
    db_session.flush()

    decision = policy_engine.decide(db_session, case, config=config, now=now)
    # CONTACT_CUSTOMER is blocked; next candidate for this category is ESCALATE.
    assert decision.chosen_action == "ESCALATE"
    assert decision.alternatives["CONTACT_CUSTOMER"]["allowed"] is False


def test_max_contacts_per_7_days_blocks_further_contact(db_session):
    case = make_case(db_session, failure_category="MANDATE_ISSUE", amount=Decimal("500"))
    config = policy_engine.GuardrailConfig(max_contacts_per_case=10, max_contacts_per_7_days=1)
    now = datetime.utcnow()

    db_session.add(Action(
        revenue_case_id=case.id, action_type="CONTACT_CUSTOMER",
        status="EXECUTED", created_at=now - timedelta(days=2),
    ))
    db_session.flush()

    decision = policy_engine.decide(db_session, case, config=config, now=now)
    assert decision.chosen_action == "ESCALATE"
    assert "max_contacts_per_7_days" in decision.alternatives["CONTACT_CUSTOMER"]["reason"]


def test_cooldown_blocks_contact_too_soon_after_last_one(db_session):
    case = make_case(db_session, failure_category="CUSTOMER_AUTHENTICATION", amount=Decimal("500"))
    config = policy_engine.GuardrailConfig(min_contact_interval_hours=12)
    now = datetime.utcnow()

    db_session.add(Action(
        revenue_case_id=case.id, action_type="CONTACT_CUSTOMER",
        status="EXECUTED", created_at=now - timedelta(hours=1),  # too recent
    ))
    db_session.flush()

    decision = policy_engine.decide(db_session, case, config=config, now=now)
    assert decision.chosen_action == "ESCALATE"
    assert "cooldown" in decision.alternatives["CONTACT_CUSTOMER"]["reason"]


def test_cooldown_clears_after_interval_elapses(db_session):
    case = make_case(db_session, failure_category="CUSTOMER_AUTHENTICATION", amount=Decimal("500"))
    config = policy_engine.GuardrailConfig(min_contact_interval_hours=12)
    now = datetime.utcnow()

    db_session.add(Action(
        revenue_case_id=case.id, action_type="CONTACT_CUSTOMER",
        status="EXECUTED", created_at=now - timedelta(hours=13),  # past the cooldown
    ))
    db_session.flush()

    decision = policy_engine.decide(db_session, case, config=config, now=now)
    assert decision.chosen_action == "CONTACT_CUSTOMER"


def test_max_total_attempts_stopping_rule(db_session):
    from app.models import AuditEvent

    case = make_case(db_session, failure_category="TRANSIENT_INFRASTRUCTURE", amount=Decimal("500"))
    config = policy_engine.GuardrailConfig(max_total_attempts=2)

    for _ in range(3):
        db_session.add(AuditEvent(revenue_case_id=case.id, event="failure_event_received", detail={}))
    db_session.flush()

    decision = policy_engine.decide(db_session, case, config=config)
    assert decision.chosen_action == "STOP"
    assert case.state == "STOPPED"


def test_every_decision_has_an_explanation(db_session):
    case = make_case(db_session, failure_category="PERMANENT_HARD_FAILURE", amount=Decimal("100"))
    decision = policy_engine.decide(db_session, case)
    assert decision.explanation
    assert decision.chosen_action in decision.explanation


def test_decision_creates_a_matching_scheduled_action(db_session):
    case = make_case(db_session, failure_category="TRANSIENT_INFRASTRUCTURE", amount=Decimal("100"))
    decision = policy_engine.decide(db_session, case)

    action = (
        db_session.query(Action)
        .filter(Action.revenue_case_id == case.id, Action.decision_id == decision.id)
        .one()
    )
    assert action.action_type == decision.chosen_action
    assert action.status == "SCHEDULED"
