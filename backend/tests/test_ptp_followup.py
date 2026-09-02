from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.database import SessionLocal
from app.models import RevenueCase, Action, PromiseToPay
from app.services import ptp_followup

NOW = datetime(2026, 8, 30, 9, 0, 0)


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_case_with_due_promise(db, state="AWAITING_OUTCOME", promise_status="PENDING", due_offset_hours=-1):
    case = RevenueCase(source="synthetic", amount=Decimal("5000"), currency="INR", state=state)
    db.add(case)
    db.flush()

    promise = PromiseToPay(
        revenue_case_id=case.id, promised_amount=Decimal("5000"),
        promised_date=NOW + timedelta(hours=due_offset_hours), status=promise_status,
    )
    db.add(promise)

    action = Action(
        revenue_case_id=case.id, action_type="FOLLOW_UP_PTP", status="SCHEDULED",
        scheduled_for=NOW + timedelta(hours=due_offset_hours),
    )
    db.add(action)
    db.flush()
    return case, promise, action


def test_broken_promise_escalates_case_and_marks_promise_broken(db_session):
    case, promise, action = make_case_with_due_promise(db_session)

    summary = ptp_followup.process_due_followups(db_session, now=NOW)

    assert summary == {"processed": 1, "already_recovered": 0, "broken": 1}
    assert case.state == "HUMAN_REVIEW"
    assert promise.status == "BROKEN"
    assert action.status == "EXECUTED"


def test_already_recovered_case_is_left_alone(db_session):
    case, promise, action = make_case_with_due_promise(db_session, state="RECOVERED")

    ptp_followup.process_due_followups(db_session, now=NOW)

    assert case.state == "RECOVERED"  # untouched
    assert promise.status == "PENDING"  # untouched — no need to mark it broken, the case already recovered
    assert action.status == "EXECUTED"  # still marked done, so it's not reprocessed


def test_future_followup_is_not_processed_yet(db_session):
    case, promise, action = make_case_with_due_promise(db_session, due_offset_hours=+5)  # not due yet

    ptp_followup.process_due_followups(db_session, now=NOW)

    assert action.status == "SCHEDULED"  # untouched — not due
    assert promise.status == "PENDING"
    assert case.state == "AWAITING_OUTCOME"


def test_followup_exactly_at_due_time_is_processed(db_session):
    case, promise, action = make_case_with_due_promise(db_session, due_offset_hours=0)

    ptp_followup.process_due_followups(db_session, now=NOW)

    assert action.status == "EXECUTED"
    assert promise.status == "BROKEN"


def test_already_processed_followup_is_not_reprocessed(db_session):
    case, promise, action = make_case_with_due_promise(db_session)
    ptp_followup.process_due_followups(db_session, now=NOW)
    assert promise.status == "BROKEN"

    # Simulate the case having since been manually recovered — if the
    # action were (wrongly) reprocessed, this would flip back to
    # HUMAN_REVIEW. It shouldn't, because the action is now EXECUTED.
    case.state = "RECOVERED"
    ptp_followup.process_due_followups(db_session, now=NOW)
    assert case.state == "RECOVERED"


def test_multiple_due_followups_in_one_batch_are_all_processed(db_session):
    _, promise_a, action_a = make_case_with_due_promise(db_session)
    _, promise_b, action_b = make_case_with_due_promise(db_session)
    case_c, promise_c, action_c = make_case_with_due_promise(db_session, state="RECOVERED")

    ptp_followup.process_due_followups(db_session, now=NOW)

    assert action_a.status == "EXECUTED" and promise_a.status == "BROKEN"
    assert action_b.status == "EXECUTED" and promise_b.status == "BROKEN"
    assert action_c.status == "EXECUTED" and promise_c.status == "PENDING"  # recovered case, promise left alone
