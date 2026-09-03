from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Action, PaymentEvent, PromiseToPay, RevenueCase
from app.services import llm_client, orchestrator, razorpay_client, task_queue, temporal_runtime
from app.worker import WindowsWorker
from rq.timeouts import TimerDeathPenalty


NOW = datetime(2026, 9, 3, 10, 0, 0)


def make_action(action_type="CREATE_PAYMENT_LINK", state="ACTION_SCHEDULED", max_attempts=3):
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="razorpay",
            amount=Decimal("1200"),
            currency="INR",
            failure_category="INVALID_INSTRUMENT",
            state=state,
        )
        db.add(case)
        db.flush()
        action = Action(
            revenue_case_id=case.id,
            action_type=action_type,
            status="SCHEDULED",
            scheduled_for=NOW,
            max_attempts=max_attempts,
        )
        db.add(action)
        db.flush()
        return case.id, action.id


def test_duplicate_job_delivery_executes_external_action_once(monkeypatch):
    case_id, action_id = make_action()
    calls = []

    def create_link(**kwargs):
        calls.append(kwargs)
        return {"id": "plink_once", "short_url": "https://example.test/once", "simulated": True}

    monkeypatch.setattr(razorpay_client, "create_payment_link", create_link)

    assert temporal_runtime.process_action(action_id, now=NOW) == "executed"
    assert temporal_runtime.process_action(action_id, now=NOW) == "stale"
    assert len(calls) == 1
    assert calls[0]["reference_id"] == action_id

    with SessionLocal() as db:
        action = db.get(Action, action_id)
        case = db.get(RevenueCase, case_id)
        assert action.status == "EXECUTED"
        assert action.attempt_count == 1
        assert case.state == "AWAITING_OUTCOME"


def test_external_failure_retries_then_fails_with_sanitized_error(monkeypatch):
    case_id, action_id = make_action(max_attempts=2)
    monkeypatch.setattr(settings, "ACTION_RETRY_DELAY_SECONDS", 0)

    def fail(**kwargs):
        raise razorpay_client.RazorpayAPIError("provider response containing sensitive detail")

    monkeypatch.setattr(razorpay_client, "create_payment_link", fail)

    assert temporal_runtime.process_action(action_id, now=NOW) == "retry_scheduled"
    assert temporal_runtime.process_action(action_id, now=NOW) == "failed"

    with SessionLocal() as db:
        action = db.get(Action, action_id)
        case = db.get(RevenueCase, case_id)
        assert action.status == "FAILED"
        assert action.attempt_count == 2
        assert action.last_error == "external service request failed"
        assert "sensitive" not in str(action.result)
        assert case.state == "HUMAN_REVIEW"


def test_ineligible_external_action_is_cancelled_before_provider_call(monkeypatch):
    _, action_id = make_action(state="HUMAN_REVIEW")

    def should_not_run(**kwargs):
        raise AssertionError("stale action reached provider")

    monkeypatch.setattr(razorpay_client, "create_payment_link", should_not_run)
    assert temporal_runtime.process_action(action_id, now=NOW) == "stale"
    with SessionLocal() as db:
        assert db.get(Action, action_id).status == "CANCELLED"


def test_wait_wakeup_redecides_and_duplicate_delivery_is_stale():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="synthetic",
            amount=Decimal("500"),
            currency="INR",
            failure_category="INSUFFICIENT_BALANCE",
            state="WAITING",
        )
        db.add(case)
        db.flush()
        wait = Action(
            revenue_case_id=case.id,
            action_type="WAIT",
            status="SCHEDULED",
            scheduled_for=NOW,
        )
        db.add(wait)
        db.flush()
        case_id, action_id = case.id, wait.id

    assert temporal_runtime.process_action(action_id, now=NOW) == "executed"
    assert temporal_runtime.process_action(action_id, now=NOW) == "stale"

    with SessionLocal() as db:
        case = db.get(RevenueCase, case_id)
        actions = db.query(Action).filter(Action.revenue_case_id == case_id).all()
        assert case.state == "ACTION_SCHEDULED"
        assert next(a for a in actions if a.id == action_id).status == "EXECUTED"
        assert any(a.action_type == "COLLECT_PROMISE_TO_PAY" and a.status == "SCHEDULED" for a in actions)


def test_followup_breaks_only_its_linked_promise():
    with SessionLocal.begin() as db:
        case = RevenueCase(source="synthetic", amount=Decimal("900"), currency="INR", state="AWAITING_OUTCOME")
        db.add(case)
        db.flush()
        first = PromiseToPay(
            revenue_case_id=case.id,
            promised_amount=Decimal("400"),
            promised_date=NOW,
            status="PENDING",
        )
        second = PromiseToPay(
            revenue_case_id=case.id,
            promised_amount=Decimal("500"),
            promised_date=NOW,
            status="PENDING",
        )
        db.add_all([first, second])
        db.flush()
        action = Action(
            revenue_case_id=case.id,
            promise_to_pay_id=first.id,
            action_type="FOLLOW_UP_PTP",
            status="SCHEDULED",
            scheduled_for=NOW,
        )
        db.add(action)
        db.flush()
        first_id, second_id, action_id = first.id, second.id, action.id

    assert temporal_runtime.process_action(action_id, now=NOW) == "broken"
    with SessionLocal() as db:
        assert db.get(PromiseToPay, first_id).status == "BROKEN"
        assert db.get(PromiseToPay, second_id).status == "PENDING"


def test_recovery_reopens_stopped_case_and_keeps_pending_promise():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_stopped",
            amount=Decimal("750"),
            currency="INR",
            state="STOPPED",
        )
        db.add(case)
        db.flush()
        promise = PromiseToPay(
            revenue_case_id=case.id,
            promised_amount=Decimal("750"),
            promised_date=NOW,
            status="PENDING",
        )
        db.add(promise)
        db.flush()
        followup = Action(
            revenue_case_id=case.id,
            promise_to_pay_id=promise.id,
            action_type="FOLLOW_UP_PTP",
            status="SCHEDULED",
            scheduled_for=NOW,
        )
        event = PaymentEvent(razorpay_event_id="evt_stopped_recovery", event_type="payment.captured")
        db.add_all([followup, event])
        db.flush()
        payload = {"payload": {"payment": {"entity": {"entity": "payment", "id": "pay_stopped"}}}}
        recovered = orchestrator.handle_event(db, event, "payment.captured", payload)
        case_id, promise_id, action_id = recovered.id, promise.id, followup.id

    with SessionLocal() as db:
        assert db.get(RevenueCase, case_id).state == "RECOVERED"
        assert db.get(PromiseToPay, promise_id).status == "KEPT"
        assert db.get(Action, action_id).status == "CANCELLED"


def test_repeated_failure_cancels_stale_wait_and_redecides():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_repeat",
            amount=Decimal("1000"),
            currency="INR",
            failure_category="TRANSIENT_INFRASTRUCTURE",
            state="WAITING",
        )
        db.add(case)
        db.flush()
        old_wait = Action(
            revenue_case_id=case.id,
            action_type="WAIT",
            status="SCHEDULED",
            scheduled_for=NOW + timedelta(hours=1),
        )
        event = PaymentEvent(razorpay_event_id="evt_repeat", event_type="payment.failed")
        db.add_all([old_wait, event])
        db.flush()
        payload = {
            "payload": {
                "payment": {
                    "entity": {
                        "entity": "payment",
                        "id": "pay_repeat",
                        "amount": 100000,
                        "error_reason": "card_expired",
                    }
                }
            }
        }
        updated = orchestrator.handle_event(db, event, "payment.failed", payload)
        case_id, old_wait_id = updated.id, old_wait.id

    with SessionLocal() as db:
        case = db.get(RevenueCase, case_id)
        assert db.get(Action, old_wait_id).status == "CANCELLED"
        assert case.failure_category == "INVALID_INSTRUMENT"
        assert case.state == "ACTION_SCHEDULED"
        assert db.query(Action).filter(
            Action.revenue_case_id == case_id,
            Action.action_type == "CREATE_PAYMENT_LINK",
            Action.status == "SCHEDULED",
        ).count() == 1


def test_claimed_ptp_cannot_overwrite_newer_human_review_state():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_ptp_race",
            amount=Decimal("1000"),
            currency="INR",
            state="AWAITING_OUTCOME",
        )
        db.add(case)
        db.flush()
        promise = PromiseToPay(
            revenue_case_id=case.id,
            promised_amount=Decimal("1000"),
            promised_date=NOW,
            status="PENDING",
        )
        db.add(promise)
        db.flush()
        followup = Action(
            revenue_case_id=case.id,
            promise_to_pay_id=promise.id,
            action_type="FOLLOW_UP_PTP",
            status="EXECUTING",
            scheduled_for=NOW,
            claimed_at=NOW,
            attempt_count=1,
        )
        db.add(followup)
        db.flush()
        updated = orchestrator.handle_customer_reply(
            db,
            case,
            "unclear response",
            now=NOW,
            business_now=NOW,
            extraction=llm_client.PTPExtraction(intent="unclear", confidence=0.2),
        )
        case_id, action_id, promise_id = updated.id, followup.id, promise.id

    assert temporal_runtime._complete_ptp(action_id, NOW) == "stale"
    with SessionLocal() as db:
        assert db.get(RevenueCase, case_id).state == "HUMAN_REVIEW"
        assert db.get(Action, action_id).status == "CANCELLED"
        assert db.get(PromiseToPay, promise_id).status == "PENDING"


def test_repeated_failure_preserves_pending_promise_until_deadline():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_promised_retry",
            amount=Decimal("1000"),
            currency="INR",
            state="AWAITING_OUTCOME",
        )
        db.add(case)
        db.flush()
        promise = PromiseToPay(
            revenue_case_id=case.id,
            promised_amount=Decimal("1000"),
            promised_date=NOW + timedelta(days=1),
            status="PENDING",
        )
        db.add(promise)
        db.flush()
        followup = Action(
            revenue_case_id=case.id,
            promise_to_pay_id=promise.id,
            action_type="FOLLOW_UP_PTP",
            status="SCHEDULED",
            scheduled_for=NOW + timedelta(days=2),
        )
        event = PaymentEvent(razorpay_event_id="evt_promised_retry", event_type="payment.failed")
        db.add_all([followup, event])
        db.flush()
        updated = orchestrator.handle_event(
            db,
            event,
            "payment.failed",
            {
                "payload": {
                    "payment": {
                        "entity": {
                            "entity": "payment",
                            "id": "pay_promised_retry",
                            "error_reason": "card_expired",
                        }
                    }
                }
            },
        )
        case_id, promise_id, action_id = updated.id, promise.id, followup.id

    with SessionLocal() as db:
        assert db.get(RevenueCase, case_id).state == "AWAITING_OUTCOME"
        assert db.get(PromiseToPay, promise_id).status == "PENDING"
        assert db.get(Action, action_id).status == "SCHEDULED"


def test_human_review_does_not_resume_automation_on_repeated_failure():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_human_review",
            amount=Decimal("1000"),
            currency="INR",
            state="HUMAN_REVIEW",
        )
        event = PaymentEvent(razorpay_event_id="evt_human_review", event_type="payment.failed")
        db.add_all([case, event])
        db.flush()
        updated = orchestrator.handle_event(
            db,
            event,
            "payment.failed",
            {"payload": {"payment": {"entity": {"entity": "payment", "id": "pay_human_review"}}}},
        )
        case_id = updated.id

    with SessionLocal() as db:
        assert db.get(RevenueCase, case_id).state == "HUMAN_REVIEW"
        assert db.query(Action).filter(Action.revenue_case_id == case_id).count() == 0


def test_new_promise_supersedes_old_followup_and_uses_end_of_local_day():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="synthetic",
            amount=Decimal("1000"),
            currency="INR",
            state="AWAITING_OUTCOME",
        )
        db.add(case)
        db.flush()
        old_promise = PromiseToPay(
            revenue_case_id=case.id,
            promised_amount=Decimal("500"),
            promised_date=datetime(2026, 9, 2),
            status="PENDING",
        )
        db.add(old_promise)
        db.flush()
        old_action = Action(
            revenue_case_id=case.id,
            promise_to_pay_id=old_promise.id,
            action_type="FOLLOW_UP_PTP",
            status="SCHEDULED",
            scheduled_for=NOW,
        )
        db.add(old_action)
        db.flush()
        extraction = llm_client.PTPExtraction(
            intent="promise_to_pay",
            amount=1000,
            date="2026-09-03",
            confidence=0.9,
        )
        orchestrator.handle_customer_reply(
            db,
            case,
            "I'll pay 1000 today",
            now=datetime(2026, 9, 2, 20, 0),
            business_now=datetime(2026, 9, 3, 1, 30),
            extraction=extraction,
        )
        case_id, old_promise_id, old_action_id = case.id, old_promise.id, old_action.id

    with SessionLocal() as db:
        assert db.get(PromiseToPay, old_promise_id).status == "SUPERSEDED"
        assert db.get(Action, old_action_id).status == "CANCELLED"
        new_action = (
            db.query(Action)
            .filter(Action.revenue_case_id == case_id, Action.id != old_action_id)
            .one()
        )
        assert new_action.promise_to_pay_id is not None
        assert new_action.scheduled_for == datetime(2026, 9, 3, 18, 30)


def test_subscription_events_correlate_by_subscription_entity():
    with SessionLocal.begin() as db:
        halted = PaymentEvent(razorpay_event_id="evt_sub_halted", event_type="subscription.halted")
        db.add(halted)
        db.flush()
        case = orchestrator.handle_event(
            db,
            halted,
            "subscription.halted",
            {
                "payload": {
                    "subscription": {
                        "entity": {
                            "entity": "subscription",
                            "id": "sub_temporal",
                            "error_reason": "subscription_halted",
                        }
                    }
                }
            },
        )
        case_id = case.id

    with SessionLocal.begin() as db:
        charged = PaymentEvent(razorpay_event_id="evt_sub_charged", event_type="subscription.charged")
        db.add(charged)
        db.flush()
        recovered = orchestrator.handle_event(
            db,
            charged,
            "subscription.charged",
            {
                "payload": {
                    "subscription": {"entity": {"entity": "subscription", "id": "sub_temporal"}},
                    "payment": {
                        "entity": {
                            "entity": "payment",
                            "id": "pay_sub_temporal",
                            "subscription_id": "sub_temporal",
                        }
                    },
                }
            },
        )
        assert recovered.id == case_id
        assert recovered.state == "RECOVERED"


def test_new_payment_cycle_does_not_reuse_recovered_subscription_case():
    with SessionLocal.begin() as db:
        old_case = RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_old_cycle",
            razorpay_subscription_id="sub_reused_cycle",
            amount=Decimal("500"),
            state="RECOVERED",
        )
        event = PaymentEvent(razorpay_event_id="evt_new_cycle", event_type="payment.failed")
        db.add_all([old_case, event])
        db.flush()
        new_case = orchestrator.handle_event(
            db,
            event,
            "payment.failed",
            {
                "payload": {
                    "payment": {
                        "entity": {
                            "entity": "payment",
                            "id": "pay_new_cycle",
                            "subscription_id": "sub_reused_cycle",
                            "amount": 70000,
                            "error_reason": "card_expired",
                        }
                    }
                }
            },
        )
        assert new_case.id != old_case.id
        assert new_case.razorpay_payment_id == "pay_new_cycle"
        assert new_case.amount == Decimal("700")


def test_payment_attempt_merges_into_existing_open_subscription_case():
    with SessionLocal.begin() as db:
        existing = RevenueCase(
            source="razorpay",
            razorpay_subscription_id="sub_open_attempt",
            amount=Decimal("0"),
            state="WAITING",
        )
        event = PaymentEvent(razorpay_event_id="evt_open_attempt", event_type="payment.failed")
        db.add_all([existing, event])
        db.flush()
        updated = orchestrator.handle_event(
            db,
            event,
            "payment.failed",
            {
                "payload": {
                    "payment": {
                        "entity": {
                            "entity": "payment",
                            "id": "pay_open_attempt",
                            "subscription_id": "sub_open_attempt",
                            "amount": 65000,
                            "error_reason": "card_expired",
                        }
                    }
                }
            },
        )
        assert updated.id == existing.id
        assert updated.razorpay_payment_id == "pay_open_attempt"
        assert updated.amount == Decimal("650")
        assert db.query(RevenueCase).filter(
            RevenueCase.source == "razorpay",
            RevenueCase.razorpay_subscription_id == "sub_open_attempt",
        ).count() == 1


def test_razorpay_provider_ids_are_unique_without_blocking_experiment_clones():
    with SessionLocal.begin() as db:
        db.add(RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_unique_runtime",
            amount=Decimal("1"),
            state="DETECTED",
        ))

    with SessionLocal() as db:
        db.add(RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_unique_runtime",
            amount=Decimal("1"),
            state="DETECTED",
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    with SessionLocal.begin() as db:
        db.add_all([
            RevenueCase(
                source="experiment",
                razorpay_subscription_id="sub_shared_experiment",
                amount=Decimal("1"),
                state="DIAGNOSED",
            ),
            RevenueCase(
                source="experiment",
                razorpay_subscription_id="sub_shared_experiment",
                amount=Decimal("1"),
                state="DIAGNOSED",
            ),
        ])


def test_reconciliation_recovers_expired_claim_without_redis():
    case_id, action_id = make_action()
    with SessionLocal.begin() as db:
        action = db.get(Action, action_id)
        action.status = "EXECUTING"
        action.claimed_at = NOW - timedelta(minutes=10)
        action.attempt_count = 1

    summary = temporal_runtime.reconcile_actions(now=NOW)
    assert summary == {"reset": 1, "failed": 0, "scheduled": 1, "enqueued": 0}
    with SessionLocal() as db:
        action = db.get(Action, action_id)
        assert action.status == "SCHEDULED"
        assert action.last_error == "execution lease expired"


def test_reconciliation_cancels_exhausted_superseded_claim_without_overwriting_case():
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="razorpay",
            amount=Decimal("100"),
            currency="INR",
            state="ACTION_SCHEDULED",
        )
        db.add(case)
        db.flush()
        old = Action(
            revenue_case_id=case.id,
            action_type="CREATE_PAYMENT_LINK",
            status="EXECUTING",
            claimed_at=NOW - timedelta(minutes=10),
            attempt_count=3,
            max_attempts=3,
            created_at=NOW - timedelta(hours=1),
        )
        newer = Action(
            revenue_case_id=case.id,
            action_type="CONTACT_CUSTOMER",
            status="SCHEDULED",
            scheduled_for=NOW,
            created_at=NOW,
        )
        db.add_all([old, newer])
        db.flush()
        case_id, old_id, newer_id = case.id, old.id, newer.id

    summary = temporal_runtime.reconcile_actions(now=NOW)
    assert summary["failed"] == 0
    with SessionLocal() as db:
        assert db.get(RevenueCase, case_id).state == "ACTION_SCHEDULED"
        assert db.get(Action, old_id).status == "CANCELLED"
        assert db.get(Action, newer_id).status == "SCHEDULED"


def test_queue_payload_contains_only_action_id(monkeypatch):
    _, action_id = make_action()
    published = []

    class FakeQueue:
        def fetch_job(self, job_id):
            return None

        def enqueue(self, function, *args, **kwargs):
            published.append((function, args, kwargs))

        def enqueue_at(self, scheduled_for, function, *args, **kwargs):
            published.append((function, args, kwargs))

    monkeypatch.setattr(settings, "TASK_QUEUE_ENABLED", True)
    monkeypatch.setattr(task_queue, "get_queue", lambda: FakeQueue())

    assert task_queue.enqueue_action(action_id) is True
    assert published[0][0] == "app.jobs.process_action"
    assert published[0][1] == (action_id,)
    assert published[0][2]["job_id"] == f"action-{action_id}-0"


def test_orphaned_rq_record_does_not_mask_scheduled_database_action(monkeypatch):
    _, action_id = make_action()
    published = []

    class ExistingJob:
        deleted = False

        def delete(self):
            self.deleted = True

    existing = ExistingJob()

    class FakeQueue:
        def fetch_job(self, job_id):
            return existing

        def enqueue(self, function, *args, **kwargs):
            published.append((function, args, kwargs))

        def enqueue_at(self, scheduled_for, function, *args, **kwargs):
            published.append((function, args, kwargs))

    monkeypatch.setattr(settings, "TASK_QUEUE_ENABLED", True)
    monkeypatch.setattr(task_queue, "get_queue", lambda: FakeQueue())
    monkeypatch.setattr(task_queue, "_job_is_registered", lambda queue, job_id: False)

    assert task_queue.enqueue_action(action_id) is True
    assert existing.deleted is True
    assert published[0][1] == (action_id,)


def test_reconciliation_rotates_through_batches(monkeypatch):
    action_ids = [make_action()[1] for _ in range(3)]
    published = []

    class FakeQueue:
        name = "test"
        connection = object()
        job_ids = []

        def fetch_job(self, job_id):
            return None

        def enqueue(self, function, *args, **kwargs):
            published.append(args[0])

        def enqueue_at(self, scheduled_for, function, *args, **kwargs):
            published.append(args[0])

    monkeypatch.setattr(settings, "TASK_QUEUE_ENABLED", True)
    monkeypatch.setattr(task_queue, "get_queue", lambda: FakeQueue())

    first = temporal_runtime.reconcile_actions(now=NOW, limit=2)
    second = temporal_runtime.reconcile_actions(now=NOW, limit=2)
    assert first["scheduled"] == 2
    assert second["scheduled"] == 2
    assert set(action_ids).issubset(set(published))


def test_reconciliation_never_enqueues_experiment_actions(monkeypatch):
    with SessionLocal.begin() as db:
        case = RevenueCase(
            source="experiment",
            amount=Decimal("100"),
            currency="INR",
            state="ACTION_SCHEDULED",
        )
        db.add(case)
        db.flush()
        db.add(Action(
            revenue_case_id=case.id,
            action_type="CREATE_PAYMENT_LINK",
            status="SCHEDULED",
            scheduled_for=NOW,
        ))

    monkeypatch.setattr(settings, "TASK_QUEUE_ENABLED", True)
    monkeypatch.setattr(task_queue, "enqueue_action", lambda action_id: pytest.fail("experiment action enqueued"))
    assert temporal_runtime.reconcile_actions(now=NOW)["scheduled"] == 0


def test_windows_worker_uses_cross_platform_timeout():
    assert WindowsWorker.death_penalty_class is TimerDeathPenalty


def test_prior_recovery_does_not_suppress_unrelated_subscription_cycle():
    # Cycle A succeeds, leaving an unlinked subscription.charged with pay_A
    with SessionLocal.begin() as db:
        recovery = PaymentEvent(
            razorpay_event_id="evt_cycle_a_recovery",
            event_type="subscription.charged",
            razorpay_payment_id="pay_cycle_a",
            razorpay_subscription_id="sub_shared_cycle",
        )
        db.add(recovery)

    # Cycle B later fails via subscription.halted for same subscription
    # (different receivable) — must NOT be suppressed by cycle A's recovery.
    with SessionLocal.begin() as db:
        failure = PaymentEvent(razorpay_event_id="evt_cycle_b_failure", event_type="subscription.halted")
        db.add(failure)
        db.flush()
        case = orchestrator.handle_event(
            db,
            failure,
            "subscription.halted",
            {"payload": {"subscription": {"entity": {"entity": "subscription", "id": "sub_shared_cycle"}}}},
        )
        assert case is not None
        assert case.state != "RECOVERED"
        assert case.razorpay_subscription_id == "sub_shared_cycle"
        # recovery remains unlinked, failure got its own case and was not suppressed
        assert db.query(PaymentEvent).filter(PaymentEvent.razorpay_event_id == "evt_cycle_a_recovery").one().revenue_case_id is None
        assert db.query(PaymentEvent).filter(PaymentEvent.razorpay_event_id == "evt_cycle_b_failure").one().revenue_case_id == case.id
