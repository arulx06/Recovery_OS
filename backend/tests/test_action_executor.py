from decimal import Decimal

import pytest

from app.core.database import SessionLocal
from app.models import RevenueCase, Action, Decision, CustomerMessage
from app.services import action_executor, razorpay_client, llm_client


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_case_with_scheduled_action(db, action_type="CREATE_PAYMENT_LINK", amount=Decimal("500")) -> tuple[RevenueCase, Action]:
    case = RevenueCase(
        source="synthetic", amount=amount, currency="INR",
        failure_category="INVALID_INSTRUMENT", state="ACTION_SCHEDULED",
    )
    db.add(case)
    db.flush()

    decision = Decision(revenue_case_id=case.id, chosen_action=action_type, explanation="test")
    db.add(decision)
    db.flush()

    action = Action(revenue_case_id=case.id, decision_id=decision.id, action_type=action_type, status="SCHEDULED")
    db.add(action)
    db.flush()
    return case, action


def test_execute_create_payment_link_success(db_session):
    case, action = make_case_with_scheduled_action(db_session, amount=Decimal("750.25"))

    action_executor.execute(db_session, case, action)

    assert action.status == "EXECUTED"
    assert action.executed_at is not None
    assert action.result["simulated"] is True
    assert case.razorpay_payment_link_id == action.result["id"]
    assert case.state == "AWAITING_OUTCOME"


def test_execute_records_audit_event(db_session):
    from app.models import AuditEvent

    case, action = make_case_with_scheduled_action(db_session)
    action_executor.execute(db_session, case, action)

    events = db_session.query(AuditEvent).filter(AuditEvent.revenue_case_id == case.id).all()
    assert any(e.event == "action_executed" for e in events)


def test_execute_handles_live_api_failure_gracefully(db_session, monkeypatch):
    case, action = make_case_with_scheduled_action(db_session)

    def raise_error(*args, **kwargs):
        raise razorpay_client.RazorpayAPIError("simulated network failure")

    monkeypatch.setattr(razorpay_client, "create_payment_link", raise_error)

    action_executor.execute(db_session, case, action)

    assert action.status == "FAILED"
    assert "error" in action.result
    assert case.state == "HUMAN_REVIEW"


def test_execute_is_noop_for_non_executable_action_types(db_session):
    case, action = make_case_with_scheduled_action(db_session, action_type="WAIT")
    case.state = "WAITING"

    result = action_executor.execute(db_session, case, action)

    assert result.status == "SCHEDULED"  # untouched
    assert case.state == "WAITING"  # untouched


def test_execute_is_noop_for_already_executed_action(db_session):
    case, action = make_case_with_scheduled_action(db_session)
    action.status = "EXECUTED"  # already done, e.g. re-processed event

    action_executor.execute(db_session, case, action)

    assert action.result is None  # untouched — execute() didn't run again


# ---- Phase 6: CONTACT_CUSTOMER / COLLECT_PROMISE_TO_PAY execution ----

@pytest.mark.parametrize("action_type", ["CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY"])
def test_execute_contact_actions_draft_and_store_a_message(db_session, action_type):
    case, action = make_case_with_scheduled_action(db_session, action_type=action_type, amount=Decimal("2500"))

    action_executor.execute(db_session, case, action)

    assert action.status == "EXECUTED"
    assert action.result["simulated"] is True
    assert case.state == "AWAITING_OUTCOME"

    messages = db_session.query(CustomerMessage).filter(CustomerMessage.revenue_case_id == case.id).all()
    assert len(messages) == 1
    assert messages[0].direction == "outbound"
    assert messages[0].channel == "simulated"
    assert messages[0].body == action.result["body"]


def test_execute_contact_action_handles_live_api_failure_gracefully(db_session, monkeypatch):
    case, action = make_case_with_scheduled_action(db_session, action_type="CONTACT_CUSTOMER")

    def raise_error(*args, **kwargs):
        raise llm_client.LLMAPIError("simulated LLM outage")

    monkeypatch.setattr(llm_client, "draft_contact_message", raise_error)

    action_executor.execute(db_session, case, action)

    assert action.status == "FAILED"
    assert "error" in action.result
    assert case.state == "HUMAN_REVIEW"

    messages = db_session.query(CustomerMessage).filter(CustomerMessage.revenue_case_id == case.id).all()
    assert len(messages) == 0  # nothing recorded as "sent" if drafting failed
