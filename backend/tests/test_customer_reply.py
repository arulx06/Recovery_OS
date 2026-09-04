import hashlib
import hmac
import json

from app.core.database import SessionLocal
from app.models import Action
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


def fail_with_contact_action(client, payment_id: str, amount: int, event_id: str) -> str:
    """otp_incorrect -> CUSTOMER_AUTHENTICATION -> baseline picks CONTACT_CUSTOMER,
    which the action executor runs, landing the case in
    AWAITING_OUTCOME with an outbound message on record. Returns the case id."""
    resp = send_webhook(
        client, "payment.failed",
        {"id": payment_id, "amount": amount, "error_reason": "otp_incorrect"},
        event_id=event_id,
    )
    assert resp.json()["case_state"] == "ACTION_SCHEDULED"
    case_id = resp.json()["case_id"]
    with SessionLocal() as db:
        action_id = (
            db.query(Action.id)
            .filter(Action.revenue_case_id == case_id, Action.action_type == "CONTACT_CUSTOMER")
            .scalar()
        )
    assert temporal_runtime.process_action(action_id) == "executed"
    return case_id


# ---- the exit-criteria scenarios ----

def test_clear_promise_gets_recorded_and_a_followup_scheduled(client):
    case_id = fail_with_contact_action(client, "pay_reply_001", 800000, "evt_r001")  # ₹8,000

    resp = client.post(f"/cases/{case_id}/customer-reply", json={"body": "I'll pay 8000 Friday"})
    assert resp.status_code == 200
    assert resp.json()["case_state"] == "AWAITING_OUTCOME"

    detail = client.get(f"/cases/{case_id}").json()
    assert len(detail["promises_to_pay"]) == 1
    promise = detail["promises_to_pay"][0]
    assert promise["promised_amount"] == 8000.0
    assert promise["status"] == "PENDING"

    followups = [a for a in detail["actions"] if a["action_type"] == "FOLLOW_UP_PTP"]
    assert len(followups) == 1
    assert followups[0]["status"] == "SCHEDULED"

    inbound = [m for m in detail["messages"] if m["direction"] == "inbound"]
    assert len(inbound) == 1
    assert inbound[0]["extracted"]["intent"] == "promise_to_pay"


def test_dispute_reply_stops_recovery_immediately(client):
    case_id = fail_with_contact_action(client, "pay_reply_002", 500000, "evt_r002")

    resp = client.post(
        f"/cases/{case_id}/customer-reply",
        json={"body": "this is wrong, I already paid this last week"},
    )
    assert resp.status_code == 200
    assert resp.json()["case_state"] == "DISPUTED"

    detail = client.get(f"/cases/{case_id}").json()
    assert detail["state"] == "DISPUTED"
    assert detail["audit_trail"][-1]["event"] == "case_disputed"
    # No FOLLOW_UP_PTP should exist for a disputed case.
    assert not any(a["action_type"] == "FOLLOW_UP_PTP" for a in detail["actions"])


def test_dispute_overrides_even_an_existing_pending_promise(client):
    case_id = fail_with_contact_action(client, "pay_reply_003", 300000, "evt_r003")
    client.post(f"/cases/{case_id}/customer-reply", json={"body": "I'll pay 3000 tomorrow"})

    resp = client.post(f"/cases/{case_id}/customer-reply", json={"body": "wait, this is fraud, I never bought this"})
    assert resp.json()["case_state"] == "DISPUTED"

    # The FOLLOW_UP_PTP scheduled by the first reply should be cancelled.
    detail = client.get(f"/cases/{case_id}").json()
    followups = [a for a in detail["actions"] if a["action_type"] == "FOLLOW_UP_PTP"]
    assert followups[0]["status"] == "CANCELLED"


def test_amount_exceeding_outstanding_falls_back_to_human_review(client):
    case_id = fail_with_contact_action(client, "pay_reply_004", 100000, "evt_r004")  # ₹1,000 outstanding

    resp = client.post(f"/cases/{case_id}/customer-reply", json={"body": "I'll pay 5000 Friday"})  # promises more than owed
    assert resp.json()["case_state"] == "HUMAN_REVIEW"

    detail = client.get(f"/cases/{case_id}").json()
    assert len(detail["promises_to_pay"]) == 0
    assert detail["audit_trail"][-1]["event"] == "ptp_validation_failed"


def test_unclear_reply_falls_back_to_human_review(client):
    case_id = fail_with_contact_action(client, "pay_reply_005", 200000, "evt_r005")

    resp = client.post(f"/cases/{case_id}/customer-reply", json={"body": "hmm not sure"})
    assert resp.json()["case_state"] == "HUMAN_REVIEW"


def test_reply_on_terminal_case_is_recorded_but_does_not_reopen_it(client):
    case_id = fail_with_contact_action(client, "pay_reply_006", 400000, "evt_r006")

    # Recover it via the normal payment.captured path first.
    captured = send_webhook(client, "payment.captured", {"id": "pay_reply_006", "amount": 400000}, event_id="evt_r006b")
    assert captured.json()["case_state"] == "RECOVERED"

    resp = client.post(f"/cases/{case_id}/customer-reply", json={"body": "I'll pay 4000 Friday"})
    assert resp.json()["case_state"] == "RECOVERED"  # unchanged — reply arrived after the fact

    detail = client.get(f"/cases/{case_id}").json()
    assert any(m["direction"] == "inbound" for m in detail["messages"])  # still recorded
    assert len(detail["promises_to_pay"]) == 0  # but not acted on


def test_customer_reply_on_missing_case_404s(client):
    resp = client.post("/cases/does-not-exist/customer-reply", json={"body": "I'll pay Friday"})
    assert resp.status_code == 404
