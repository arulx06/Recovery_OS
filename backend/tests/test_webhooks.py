import hashlib
import hmac
import json

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Action, PaymentEvent, RevenueCase
from app.services import temporal_runtime

WEBHOOK_SECRET = "test_webhook_secret"


def sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def send_webhook(client, event_type: str, entity: dict, event_id: str):
    payload = {
        "entity": "event",
        "event": event_type,
        "payload": {"payment": {"entity": {"entity": "payment", **entity}}},
        "created_at": 1_700_000_000,
    }
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": sign(body),
            "x-razorpay-event-id": event_id,
            "content-type": "application/json",
        },
    )


def execute_action(case_id: str, action_type: str) -> str:
    with SessionLocal() as db:
        action_id = (
            db.query(Action.id)
            .filter(Action.revenue_case_id == case_id, Action.action_type == action_type)
            .order_by(Action.created_at.desc())
            .scalar()
        )
    return temporal_runtime.process_action(action_id)


def test_rejects_bad_signature(client):
    payload = {"event": "payment.failed", "payload": {}}
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": "not-the-real-signature", "x-razorpay-event-id": "evt_bad"},
    )
    assert resp.status_code == 400

    with SessionLocal() as db:
        assert db.query(PaymentEvent).count() == 0
        assert db.query(RevenueCase).count() == 0


def test_rejects_when_webhook_secret_is_missing(client, monkeypatch):
    payload = {"event": "payment.failed", "payload": {}}
    body = json.dumps(payload).encode()
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": sign(body), "x-razorpay-event-id": "evt_no_secret"},
    )
    assert resp.status_code == 400


def test_rejects_missing_event_id(client):
    payload = {"event": "payment.failed", "payload": {}}
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": sign(body)},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing x-razorpay-event-id"


def test_rejects_signed_non_object_json(client):
    body = json.dumps(["not", "an", "event"]).encode()
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": sign(body), "x-razorpay-event-id": "evt_array"},
    )
    assert resp.status_code == 400


def test_rejects_signed_null_event_type(client):
    body = json.dumps({"event": None, "payload": {}}).encode()
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": sign(body), "x-razorpay-event-id": "evt_null_type"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "webhook event must be a non-empty string"


def test_payment_failed_creates_a_case(client):
    resp = send_webhook(
        client,
        "payment.failed",
        {"id": "pay_test_001", "amount": 500000, "currency": "INR",
         "error_source": "bank", "error_step": "authorization", "error_reason": "payment_failed"},
        event_id="evt_001",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"
    # error_reason "payment_failed" doesn't match any specific taxonomy rule,
    # so this lands as UNKNOWN -> baseline policy conservatively escalates.
    assert body["case_state"] == "HUMAN_REVIEW"

    cases = client.get("/cases").json()
    matching = [c for c in cases if c["state"] == "HUMAN_REVIEW" and c["amount"] == 5000.0]
    assert matching, f"expected a HUMAN_REVIEW 5000.0 case, got: {cases}"


def test_duplicate_event_id_is_ignored(client):
    first = send_webhook(
        client, "payment.failed",
        {"id": "pay_test_002", "amount": 100000},
        event_id="evt_002",
    )
    assert first.json()["status"] == "processed"

    second = send_webhook(
        client, "payment.failed",
        {"id": "pay_test_002", "amount": 100000},
        event_id="evt_002",  # same event id — Razorpay redelivery
    )
    assert second.json()["status"] == "ignored"
    assert second.json()["reason"] == "duplicate_event"


def test_late_capture_recovers_the_case_silently(client):
    failed = send_webhook(
        client, "payment.failed",
        {"id": "pay_test_003", "amount": 250000},
        event_id="evt_003a",
    )
    # No error info given -> classifies UNKNOWN -> baseline escalates to
    # human review. The point of this test isn't which state it lands in
    # first, though — it's that a *later* capture still recovers it.
    assert failed.json()["case_state"] == "HUMAN_REVIEW"

    captured = send_webhook(
        client, "payment.captured",
        {"id": "pay_test_003", "amount": 250000},
        event_id="evt_003b",
    )
    assert captured.status_code == 200
    assert captured.json()["case_state"] == "RECOVERED"


def test_recovery_event_with_no_matching_case_is_a_noop(client):
    resp = send_webhook(
        client, "payment.captured",
        {"id": "pay_never_failed", "amount": 10000},
        event_id="evt_004",
    )
    assert resp.status_code == 200
    assert resp.json()["case_id"] is None


def test_out_of_order_capture_prevents_later_failure_automation(client):
    captured = send_webhook(
        client,
        "payment.captured",
        {"id": "pay_capture_first", "amount": 10000},
        event_id="evt_capture_first_a",
    )
    assert captured.json()["case_id"] is None

    failed = send_webhook(
        client,
        "payment.failed",
        {"id": "pay_capture_first", "amount": 10000, "error_reason": "card_expired"},
        event_id="evt_capture_first_b",
    )
    assert failed.json()["case_state"] == "RECOVERED"
    case_id = failed.json()["case_id"]
    with SessionLocal() as db:
        assert db.query(Action).filter(Action.revenue_case_id == case_id).count() == 0
        assert db.query(PaymentEvent).filter(PaymentEvent.revenue_case_id == case_id).count() == 2


def test_payment_failed_is_diagnosed_with_a_failure_category(client):
    resp = send_webhook(
        client, "payment.failed",
        {"id": "pay_test_005", "amount": 300000, "error_reason": "card_expired"},
        event_id="evt_005",
    )
    assert resp.status_code == 200
    # INVALID_INSTRUMENT's top-ranked baseline action is CREATE_PAYMENT_LINK,
    # which is unblocked (no prior contacts, amount under the automated cap)
    # and, as of Phase 4, actually gets executed (simulated, since no
    # Razorpay test-mode credentials are configured here) — landing the
    # case in AWAITING_OUTCOME rather than just ACTION_SCHEDULED.
    assert resp.json()["case_state"] == "ACTION_SCHEDULED"
    assert execute_action(resp.json()["case_id"], "CREATE_PAYMENT_LINK") == "executed"

    cases = client.get("/cases").json()
    case = next(c for c in cases if c["amount"] == 3000.0)
    assert case["failure_category"] == "INVALID_INSTRUMENT"


def send_payment_link_paid_webhook(client, link_id: str, payment_id: str, amount: int, event_id: str):
    payload = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": link_id, "entity": "payment_link", "status": "paid", "amount": amount}},
            "payment": {"entity": {"id": payment_id, "entity": "payment", "amount": amount, "status": "captured"}},
        },
        "created_at": 1_700_000_100,
    }
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": sign(body),
            "x-razorpay-event-id": event_id,
            "content-type": "application/json",
        },
    )


def test_create_payment_link_action_actually_executes_and_case_awaits_outcome(client):
    resp = send_webhook(
        client, "payment.failed",
        {"id": "pay_test_007", "amount": 90000, "error_reason": "card_expired"},
        event_id="evt_007",
    )
    assert resp.json()["case_state"] == "ACTION_SCHEDULED"

    case_id = resp.json()["case_id"]
    assert execute_action(case_id, "CREATE_PAYMENT_LINK") == "executed"
    detail = client.get(f"/cases/{case_id}").json()
    executed_actions = [a for a in detail["actions"] if a["action_type"] == "CREATE_PAYMENT_LINK"]
    assert len(executed_actions) == 1
    assert executed_actions[0]["status"] == "EXECUTED"
    assert detail["razorpay_payment_link_id"] is not None


def test_payment_link_paid_recovers_the_case(client):
    failed = send_webhook(
        client, "payment.failed",
        {"id": "pay_test_008", "amount": 60000, "error_reason": "invalid_vpa"},
        event_id="evt_008a",
    )
    case_id = failed.json()["case_id"]
    assert failed.json()["case_state"] == "ACTION_SCHEDULED"
    assert execute_action(case_id, "CREATE_PAYMENT_LINK") == "executed"

    link_id = client.get(f"/cases/{case_id}").json()["razorpay_payment_link_id"]
    assert link_id is not None

    paid = send_payment_link_paid_webhook(
        client, link_id=link_id, payment_id="pay_link_fulfillment_1", amount=60000, event_id="evt_008b",
    )
    assert paid.status_code == 200
    assert paid.json()["case_state"] == "RECOVERED"


def test_payment_link_paid_with_unknown_link_id_is_a_noop(client):
    resp = send_payment_link_paid_webhook(
        client, link_id="plink_never_created", payment_id="pay_x", amount=1000, event_id="evt_009",
    )
    assert resp.status_code == 200
    assert resp.json()["case_id"] is None
    # Fail, then recover normally first.
    send_webhook(client, "payment.failed", {"id": "pay_test_006", "amount": 75000}, event_id="evt_006a")
    recovered = send_webhook(client, "payment.captured", {"id": "pay_test_006", "amount": 75000}, event_id="evt_006b")
    assert recovered.json()["case_state"] == "RECOVERED"

    # A dispute can still land after the fact — it must override RECOVERED.
    disputed = send_webhook(
        client, "payment.dispute.created",
        {"id": "pay_test_006", "amount": 75000, "payment_id": "pay_test_006"},
        event_id="evt_006c",
    )
    assert disputed.status_code == 200
    assert disputed.json()["case_state"] == "DISPUTED"
