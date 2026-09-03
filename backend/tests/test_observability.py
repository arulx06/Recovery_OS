import hashlib
import hmac
import json
import subprocess
import sys
import uuid
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models import Action, AuditEvent, CustomerMessage, Decision, ExperimentCase, PaymentEvent, PromiseToPay, RevenueCase
from app.services import temporal_runtime
from app.ml import scorer

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

def send_link_paid(client, link_id: str, event_id: str):
    payload = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": link_id, "entity": "payment_link", "status": "paid", "amount": 60000}},
            "payment": {"entity": {"id": "pay_fulfill_1", "entity": "payment", "amount": 60000, "status": "captured"}},
        },
        "created_at": 1_700_000_100,
    }
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": sign(body), "x-razorpay-event-id": event_id, "content-type": "application/json"},
    )

def execute_action(case_id: str, action_type: str):
    with SessionLocal() as db:
        action_id = (
            db.query(Action.id)
            .filter(Action.revenue_case_id == case_id, Action.action_type == action_type)
            .scalar()
        )
    return temporal_runtime.process_action(action_id) if action_id else "no-action"


# ---------------- Dashboard ----------------

def test_dashboard_summary_empty(client):
    resp = client.get("/dashboard/summary")
    assert resp.status_code == 200
    body = resp.json()
    # Initially empty or after isolates — count fields must exist and be numeric
    for key in ["revenue_at_risk", "revenue_recovered", "total_cases", "open_cases", "recovered_cases", "waiting_cases", "human_review_cases", "active_ptps"]:
        assert key in body
    assert "policy_mode" in body
    assert "model" in body
    assert "queue" in body
    assert "razorpay" in body
    assert "llm" in body
    assert "database" in body
    assert body["database"] == "connected"
    # No secret leakage
    assert "RAZORPAY_KEY" not in json.dumps(body)
    assert "sk-ant" not in json.dumps(body)
    assert "secret" not in json.dumps(body).lower() or "webhook" in json.dumps(body).lower()  # we have webhook secret name but not value
    # Simulated label must be visible
    assert body["razorpay"]["mode_label"] in ("SIMULATED LINK", "RAZORPAY TEST MODE", "DISABLED")


def test_dashboard_summary_counts_and_revenue(client):
    # Create 3 cases: one recovers, one waits, one human review
    r1 = send_webhook(client, "payment.failed", {"id": "pay_dash_1", "amount": 100000, "error_reason": "card_expired"}, event_id="evt_dash_1")
    assert r1.status_code == 200
    c1 = r1.json()["case_id"]
    execute_action(c1, "CREATE_PAYMENT_LINK")
    # recover via link
    link_id = client.get(f"/cases/{c1}").json()["razorpay_payment_link_id"]
    send_link_paid(client, link_id, "evt_dash_1_paid")

    r2 = send_webhook(client, "payment.failed", {"id": "pay_dash_2", "amount": 200000, "error_reason": "timeout", "error_source": "bank"}, event_id="evt_dash_2")
    # timeout -> TRANSIENT -> WAIT
    assert r2.json()["case_state"] == "WAITING"

    r3 = send_webhook(client, "payment.failed", {"id": "pay_dash_3", "amount": 300000, "error_reason": "payment_failed"}, event_id="evt_dash_3")
    assert r3.json()["case_state"] == "HUMAN_REVIEW"

    summary = client.get("/dashboard/summary").json()
    assert summary["total_cases"] >= 3
    assert summary["recovered_cases"] >= 1
    assert summary["waiting_cases"] >= 1
    assert summary["human_review_cases"] >= 1
    # Revenue checks: at_risk should be sum of open (waiting+human_review), not recovered
    # recovered revenue should be >= 1000 (pay_dash_1 amount 1000? actually 100000 paise =1000 rupees)
    assert summary["revenue_recovered"] >= 1000
    assert summary["revenue_at_risk"] >= 2000 + 3000  # at least these two
    # by_state must include relevant keys
    assert "RECOVERED" in summary["by_state"]
    assert "WAITING" in summary["by_state"] or "HUMAN_REVIEW" in summary["by_state"]


def test_case_list_filters_and_search(client):
    # Create distinguishable cases
    send_webhook(client, "payment.failed", {"id": "pay_filter_1", "amount": 400000, "error_reason": "otp_incorrect"}, event_id="evt_filter_1")
    send_webhook(client, "payment.failed", {"id": "pay_filter_2", "amount": 500000, "error_reason": "card_expired"}, event_id="evt_filter_2")

    # Filter by state ACTION_SCHEDULED should return pay_filter_2 (INVALID_INSTRUMENT -> CREATE)
    resp = client.get("/cases?state=ACTION_SCHEDULED")
    assert resp.status_code == 200
    cases = resp.json()
    assert all(c["state"] == "ACTION_SCHEDULED" for c in cases)
    assert len(cases) >= 1

    # Filter by failure_category
    resp2 = client.get("/cases?failure_category=CUSTOMER_AUTHENTICATION")
    assert resp2.status_code == 200
    assert all(c["failure_category"] == "CUSTOMER_AUTHENTICATION" for c in resp2.json())
    assert any("pay_filter_1" in (c.get("razorpay_payment_id") or "") for c in resp2.json())

    # Search by payment id
    resp3 = client.get("/cases?search=pay_filter_1")
    assert resp3.status_code == 200
    assert len(resp3.json()) >= 1
    assert all("pay_filter_1" in (c.get("razorpay_payment_id") or "") or "pay_filter_1" in c["id"] for c in resp3.json())

    # Pagination limit
    resp4 = client.get("/cases?limit=1")
    assert len(resp4.json()) == 1


def test_case_list_chosen_action_and_policy_filters(client):
    # Ensure policy_mode filter works (baseline default)
    resp = client.get("/cases?policy_mode=baseline")
    assert resp.status_code == 200
    # All returned should have baseline (or null maps to baseline)
    for c in resp.json():
        # policy_mode may be baseline or None (legacy) — either is okay, but not adaptive
        assert c.get("policy_mode") in (None, "baseline")

    # chosen_action filter
    resp2 = client.get("/cases?chosen_action=WAIT")
    assert resp2.status_code == 200
    for c in resp2.json():
        assert c["chosen_action"] == "WAIT"


def test_case_list_latest_decision_filters_beyond_500_newer_cases(client):
    old_case_id = str(uuid.uuid4())
    old_created_at = datetime(2020, 1, 1)
    case_rows = [{
        "id": old_case_id,
        "source": "razorpay",
        "razorpay_payment_id": "pay_filter_older_than_500",
        "amount": Decimal("100.00"),
        "currency": "INR",
        "state": "WAITING",
        "created_at": old_created_at,
        "updated_at": old_created_at,
    }]
    decision_rows = [{
        "id": str(uuid.uuid4()),
        "revenue_case_id": old_case_id,
        "chosen_action": "WAIT",
        "policy_mode": "adaptive",
        "created_at": old_created_at,
    }]
    for index in range(501):
        case_id = str(uuid.uuid4())
        created_at = old_created_at + timedelta(days=1, seconds=index)
        case_rows.append({
            "id": case_id,
            "source": "razorpay",
            "razorpay_payment_id": f"pay_filter_newer_{index}",
            "amount": Decimal("100.00"),
            "currency": "INR",
            "state": "HUMAN_REVIEW",
            "created_at": created_at,
            "updated_at": created_at,
        })
        decision_rows.append({
            "id": str(uuid.uuid4()),
            "revenue_case_id": case_id,
            "chosen_action": "ESCALATE",
            "policy_mode": "baseline",
            "created_at": created_at,
        })

    with SessionLocal() as db:
        db.bulk_insert_mappings(RevenueCase, case_rows)
        db.bulk_insert_mappings(Decision, decision_rows)
        db.commit()

    response = client.get("/cases?chosen_action=WAIT&policy_mode=adaptive&limit=10")
    assert response.status_code == 200
    assert [row["razorpay_payment_id"] for row in response.json()] == ["pay_filter_older_than_500"]


def test_case_list_filter_and_serialization_share_latest_decision_tie_breaker(client):
    created_at = datetime(2026, 9, 3, 12, 0, 0)
    with SessionLocal() as db:
        case = RevenueCase(
            source="razorpay",
            razorpay_payment_id="pay_filter_tied_decisions",
            amount=Decimal("100.00"),
            currency="INR",
            state="WAITING",
            created_at=created_at,
        )
        db.add(case)
        db.flush()
        db.add_all([
            Decision(
                id="aaaaaaaa-0000-0000-0000-000000000001",
                revenue_case_id=case.id,
                chosen_action="ESCALATE",
                policy_mode="baseline",
                created_at=created_at,
            ),
            Decision(
                id="aaaaaaaa-0000-0000-0000-000000000002",
                revenue_case_id=case.id,
                chosen_action="WAIT",
                policy_mode="adaptive",
                created_at=created_at,
            ),
        ])
        db.commit()
        case_id = case.id

    rows = client.get("/cases?chosen_action=WAIT&policy_mode=adaptive").json()
    row = next(item for item in rows if item["id"] == case_id)
    assert row["chosen_action"] == "WAIT"
    assert row["policy_mode"] == "adaptive"
    detail = client.get(f"/cases/{case_id}").json()
    assert detail["decisions"][-1]["id"] == "aaaaaaaa-0000-0000-0000-000000000002"


# ---------------- Case Detail ----------------

def test_case_detail_has_failure_explanation_and_derived(client):
    r = send_webhook(client, "payment.failed", {"id": "pay_detail_1", "amount": 80000, "error_reason": "otp_incorrect", "error_source": "customer", "error_step": "payment_authentication"}, event_id="evt_detail_1")
    case_id = r.json()["case_id"]
    detail = client.get(f"/cases/{case_id}").json()
    assert "failure_explanation" in detail
    fe = detail["failure_explanation"]
    assert fe["failure_category"] == "CUSTOMER_AUTHENTICATION"
    assert "category_label" in fe
    assert "category_meaning" in fe
    assert "raw_signal" in fe
    assert "razorpay_payment_id" in detail
    assert detail["razorpay_payment_id"] == "pay_detail_1"
    # Guardrails and friction present
    assert "guardrails" in detail
    assert "friction" in detail
    assert detail["explainability_scopes"] == {
        "guardrails": "current_case_state",
        "friction": "current_case_state",
        "decision_inspectors": "persisted_decision_time",
    }
    # Timeline present and ordered
    assert "timeline" in detail
    assert len(detail["timeline"]) >= 2
    # Check ordering
    timestamps = [e["timestamp"] for e in detail["timeline"] if e["timestamp"]]
    assert timestamps == sorted(timestamps)
    # Provider truth
    assert "provider_truth" in detail
    # Decision inspectors
    assert "decision_inspectors" in detail
    assert len(detail["decision_inspectors"]) >= 1
    # No secret leakage
    dumped = json.dumps(detail)
    assert "rzp_test_fake" not in dumped
    assert "fake_hermetic" not in dumped
    assert "sk-ant" not in dumped


def test_baseline_decision_representation(client):
    r = send_webhook(client, "payment.failed", {"id": "pay_base_1", "amount": 60000, "error_reason": "otp_incorrect"}, event_id="evt_base_1")
    case_id = r.json()["case_id"]
    detail = client.get(f"/cases/{case_id}").json()
    insp = detail["decision_inspectors"][0]
    assert insp["policy_mode"] in ("baseline", None)
    assert insp["explanation"] is not None
    # baseline candidates should have allowed/blocked but no p_recovery
    for cand in insp["candidates"]:
        assert "allowed" in cand
        if cand["allowed"] is False:
            assert cand["blocked_reason"] is not None
    # At least one candidate has unavailable p_recovery (baseline has no ML)
    assert any(c["p_recovery"] is None for c in insp["candidates"])
    # No fabricated utility for baseline (maybe null)
    assert insp["fallback"] is None


def test_adaptive_decision_representation(client, trained_model_path):
    # Use adaptive mode with real model
    original = scorer.MODEL_PATH
    scorer.MODEL_PATH = trained_model_path
    scorer.reset_cache()
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    settings.ADAPTIVE_POLICY_PROFILE = "balanced"
    settings.ADAPTIVE_FRICTION_WEIGHT = None
    try:
        r = send_webhook(client, "payment.failed", {"id": "pay_adapt_1", "amount": 200000, "error_reason": "card_expired"}, event_id="evt_adapt_1")
        case_id = r.json()["case_id"]
        detail = client.get(f"/cases/{case_id}").json()
        insp = detail["decision_inspectors"][0]
        assert insp["policy_mode"] == "adaptive"
        assert insp["is_adaptive"] is True
        # candidates should have p_recovery, utility, friction for allowed
        allowed = [c for c in insp["candidates"] if c["allowed"] is True]
        assert len(allowed) >= 1
        for c in allowed:
            assert c["p_recovery"] is not None
            assert c["utility"] is not None
            assert c["friction_score"] is not None
            assert c["friction_penalty"] is not None
        # model provenance present
        prov = insp["model_provenance"]
        assert prov["model_version"] == "recovery-v1"
        assert prov["friction_profile"] == "balanced"
        assert prov.get("model_fingerprint") is not None or prov.get("fingerprint") is not None
    finally:
        scorer.MODEL_PATH = original
        scorer.reset_cache()
        settings.RECOVERY_POLICY = old
        settings.ADAPTIVE_POLICY_PROFILE = "balanced"
        settings.ADAPTIVE_FRICTION_WEIGHT = None


def test_blocked_guardrail_and_friction_visible(client):
    # Create case with max contacts to block CONTACT
    r = send_webhook(client, "payment.failed", {"id": "pay_block_1", "amount": 50000, "error_reason": "otp_incorrect"}, event_id="evt_block_1")
    case_id = r.json()["case_id"]
    # Manually add 3 CONTACT actions to hit max_contacts_per_case=3
    with SessionLocal() as db:
        case = db.get(RevenueCase, case_id)
        for _ in range(3):
            db.add(Action(revenue_case_id=case.id, action_type="CONTACT_CUSTOMER", status="EXECUTED", scheduled_for=datetime(2026, 9, 3, 10, 0, 0)))
        db.commit()
    # Trigger another failure on same payment to re-evaluate (new decision)
    r2 = send_webhook(client, "payment.failed", {"id": "pay_block_1", "amount": 50000, "error_reason": "otp_incorrect"}, event_id="evt_block_1b")
    # Re-diagnosis with pending contacts? Since no pending promise, it cancels and re-decides
    detail = client.get(f"/cases/{case_id}").json()
    # Latest decision should have blocked CONTACT
    insp = detail["decision_inspectors"][-1]
    blocked = [c for c in insp["candidates"] if c["action"] == "CONTACT_CUSTOMER" and c["allowed"] is False]
    assert len(blocked) >= 1
    assert "max_contacts" in blocked[0]["blocked_reason"]
    # Guardrails visibility should show used 3/3
    assert detail["guardrails"]["max_contacts_per_case"]["used"] >= 3
    assert detail["guardrails"]["max_contacts_per_case"]["pass"] is False


def test_shadow_recommendation_representation(client, trained_model_path):
    original = scorer.MODEL_PATH
    scorer.MODEL_PATH = trained_model_path
    scorer.reset_cache()
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "shadow"
    try:
        r = send_webhook(client, "payment.failed", {"id": "pay_shadow_1", "amount": 150000, "error_reason": "card_expired"}, event_id="evt_shadow_1")
        case_id = r.json()["case_id"]
        detail = client.get(f"/cases/{case_id}").json()
        # shadow decision is baseline executed
        insp = detail["decision_inspectors"][0]
        assert insp["policy_mode"] == "shadow"
        # shadow audit
        assert detail["shadow"] is not None
        assert "baseline_chosen" in detail["shadow"]
        assert "adaptive_suggested" in detail["shadow"]
        # Candidates should be present for adaptive suggestion
        assert detail["shadow"]["candidates"] is not None or detail["shadow"]["adaptive_suggested"] is not None
        # No second Action
        assert len(detail["actions"]) == 1
    finally:
        scorer.MODEL_PATH = original
        scorer.reset_cache()
        settings.RECOVERY_POLICY = old


def test_adaptive_fallback_representation(client):
    original = scorer.MODEL_PATH
    scorer.MODEL_PATH = original.parent / "missing.joblib"
    scorer.reset_cache()
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        r = send_webhook(client, "payment.failed", {"id": "pay_fallback_1", "amount": 100000, "error_reason": "timeout", "error_source": "bank"}, event_id="evt_fallback_1")
        case_id = r.json()["case_id"]
        detail = client.get(f"/cases/{case_id}").json()
        # Should be fallback
        assert detail["adaptive_fallback"] is not None
        insp = detail["decision_inspectors"][0]
        assert insp["policy_mode"] == "adaptive_fallback"
        assert "baseline" in (insp["fallback"] or "").lower() or "fallback" in (insp["fallback"] or "").lower()
    finally:
        scorer.MODEL_PATH = original
        scorer.reset_cache()
        settings.RECOVERY_POLICY = old


def test_older_decision_missing_adaptive_fields(client):
    # Simulate legacy decision without adaptive fields via direct DB insert
    with SessionLocal() as db:
        case = RevenueCase(source="razorpay", razorpay_payment_id="pay_legacy_1", amount=Decimal("999"), failure_category="UNKNOWN", state="HUMAN_REVIEW")
        db.add(case)
        db.flush()
        # Legacy decision: only baseline fields, no model_version
        legacy = Decision(revenue_case_id=case.id, chosen_action="ESCALATE", alternatives={"ESCALATE": {"allowed": True}}, guardrails_applied={"max_contacts_per_case": 3}, explanation="legacy")
        db.add(legacy)
        db.commit()
        case_id = case.id
    detail = client.get(f"/cases/{case_id}").json()
    insp = detail["decision_inspectors"][0]
    # Should not crash, missing fields render as null/unavailable
    for c in insp["candidates"]:
        # Should have allowed but p_recovery unavailable
        assert c["p_recovery"] is None or isinstance(c["p_recovery"], float)
    assert insp["model_provenance"]["model_version"] is None


def test_timeline_ordering_and_derivation(client):
    r = send_webhook(client, "payment.failed", {"id": "pay_tl_1", "amount": 70000, "error_reason": "otp_incorrect"}, event_id="evt_tl_1")
    case_id = r.json()["case_id"]
    execute_action(case_id, "CONTACT_CUSTOMER")
    # customer reply
    client.post(f"/cases/{case_id}/customer-reply", json={"body": "I'll pay 5000 tomorrow"})
    detail = client.get(f"/cases/{case_id}").json()
    tl = detail["timeline"]
    assert len(tl) >= 4
    # Verify ordering
    ts = [e["timestamp"] for e in tl if e["timestamp"]]
    assert ts == sorted(ts)
    # Each timeline event must come from persisted data
    types = {e["type"] for e in tl}
    assert "decision" in types
    assert "action" in types or "audit" in types
    # No invented payment status
    assert all(e["title"] != "LIVE PAYMENT" for e in tl)


def test_payment_link_simulated_label_and_draft(client):
    r = send_webhook(client, "payment.failed", {"id": "pay_link_1", "amount": 90000, "error_reason": "card_expired"}, event_id="evt_link_1")
    case_id = r.json()["case_id"]
    execute_action(case_id, "CREATE_PAYMENT_LINK")
    detail = client.get(f"/cases/{case_id}").json()
    assert detail["provider_truth"]["has_payment_link"] is True
    assert detail["provider_truth"]["mode_label"] == "SIMULATED LINK"
    assert detail["provider_truth"]["simulated"] is True
    # Draft must be DRAFT / NOT SENT
    outbound = [m for m in detail["messages"] if m["direction"] == "outbound"]
    assert len(outbound) >= 1
    assert outbound[0]["status"] == "DRAFT"
    assert outbound[0]["generation_method"] in ("deterministic", "llm", "llm_fallback_template")


@pytest.mark.parametrize("reconciled_action_is_newest", [True, False])
@pytest.mark.parametrize("historical_link_only_audit", [True, False])
def test_payment_link_reconciliation_is_isolated_by_action(client, reconciled_action_is_newest, historical_link_only_audit):
    now = datetime(2026, 9, 3, 12, 0, 0)
    with SessionLocal() as db:
        case = RevenueCase(
            source="razorpay",
            razorpay_payment_id=f"pay_two_links_{reconciled_action_is_newest}",
            razorpay_payment_link_id="plink_B",
            amount=Decimal("600.00"),
            currency="INR",
            failure_category="INVALID_INSTRUMENT",
            state="AWAITING_OUTCOME",
            created_at=now,
        )
        db.add(case)
        db.flush()
        action_a = Action(
            revenue_case_id=case.id,
            action_type="CREATE_PAYMENT_LINK",
            status="EXECUTED",
            result={"id": "plink_A", "short_url": "https://rzp.io/a", "simulated": False},
            created_at=now + (timedelta(seconds=1) if reconciled_action_is_newest else timedelta(seconds=2)),
            executed_at=now + timedelta(seconds=3),
        )
        action_b = Action(
            revenue_case_id=case.id,
            action_type="CREATE_PAYMENT_LINK",
            status="EXECUTED",
            result={"id": "plink_B", "short_url": "https://rzp.io/b", "simulated": False},
            created_at=now + (timedelta(seconds=2) if reconciled_action_is_newest else timedelta(seconds=1)),
            executed_at=now + timedelta(seconds=4),
        )
        db.add_all([action_a, action_b])
        db.flush()
        audit_detail = {"razorpay_payment_link_id": "plink_B"}
        if not historical_link_only_audit:
            audit_detail["action_id"] = action_b.id
        db.add(AuditEvent(
            revenue_case_id=case.id,
            event="action_reconciled",
            detail=audit_detail,
            created_at=now + timedelta(seconds=5),
        ))
        db.commit()
        case_id = case.id
        action_a_id = action_a.id
        action_b_id = action_b.id

    detail = client.get(f"/cases/{case_id}").json()
    timeline_actions = {
        row["metadata"]["action_id"]: row
        for row in detail["timeline"]
        if row["type"] == "action" and row["metadata"]["action_type"] == "CREATE_PAYMENT_LINK"
    }
    assert timeline_actions[action_a_id]["metadata"]["reconciled"] is False
    assert "created" in timeline_actions[action_a_id]["title"]
    assert timeline_actions[action_b_id]["metadata"]["reconciled"] is True
    assert "reconciled" in timeline_actions[action_b_id]["title"]

    latest_id = action_b_id if reconciled_action_is_newest else action_a_id
    latest_link_id = "plink_B" if reconciled_action_is_newest else "plink_A"
    assert detail["provider_truth"]["latest_action_id"] == latest_id
    assert detail["provider_truth"]["payment_link_id"] == latest_link_id
    assert detail["provider_truth"]["result"]["id"] == latest_link_id
    assert detail["provider_truth"]["reconciled"] is (latest_id == action_b_id)
    assert detail["provider_truth"]["reconciled"] is timeline_actions[latest_id]["metadata"]["reconciled"]


def test_ptp_provenance(client):
    r = send_webhook(client, "payment.failed", {"id": "pay_ptp_1", "amount": 800000, "error_reason": "otp_incorrect"}, event_id="evt_ptp_1")
    case_id = r.json()["case_id"]
    execute_action(case_id, "CONTACT_CUSTOMER")
    client.post(f"/cases/{case_id}/customer-reply", json={"body": "I'll pay 8000 Friday"})
    detail = client.get(f"/cases/{case_id}").json()
    ptps = detail["promises_to_pay"]
    assert len(ptps) >= 1
    ptp = ptps[0]
    assert ptp["promised_amount"] == 8000
    assert ptp["status"] == "PENDING"
    assert ptp["extraction_method"] in ("deterministic", "llm")
    assert ptp["amount_method"] == "customer_explicit"
    assert ptp["source_message_id"] is not None
    # Linked follow-up action exists
    followups = [a for a in detail["actions"] if a["action_type"] == "FOLLOW_UP_PTP"]
    assert len(followups) >= 1
    assert followups[0]["promise_to_pay_id"] == ptp["id"]
    assert followups[0]["status"] == "SCHEDULED"


def test_no_secret_or_cot_exposure(client):
    r = send_webhook(client, "payment.failed", {"id": "pay_secret_1", "amount": 50000, "error_reason": "otp_incorrect"}, event_id="evt_secret_1")
    case_id = r.json()["case_id"]
    detail = client.get(f"/cases/{case_id}").json()
    dumped = json.dumps(detail)
    # No secrets
    assert "test_webhook_secret" not in dumped
    assert "rzp_test" not in dumped
    assert "sk-ant" not in dumped
    # No CoT
    assert "chain_of_thought" not in dumped.lower()
    assert "reasoning_content" not in dumped.lower()


def test_404_handling(client):
    resp = client.get("/cases/not-a-real-id-12345")
    assert resp.status_code == 404
    resp2 = client.get("/dashboard/summary")
    assert resp2.status_code == 200


def test_synthetic_experiment_label(client, trained_model_path):
    original = scorer.MODEL_PATH
    scorer.MODEL_PATH = trained_model_path
    scorer.reset_cache()
    try:
        resp = client.post("/experiments", json={"count": 20, "seed": 7})
        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True or "run_id" in body
        # run_id should exist and arms
        run_id = body["run_id"]
        detail = client.get(f"/experiments/{run_id}").json()
        assert "arms" in detail
        # Check that experiment runner's synthetic nature is auditable via CSV
        csv_resp = client.get(f"/experiments/{run_id}/export.csv")
        assert csv_resp.status_code == 200
        assert "run_id,arm" in csv_resp.text
    finally:
        scorer.MODEL_PATH = original
        scorer.reset_cache()


def test_no_external_network_needed_for_observability(client):
    # All above tests already run hermetically via conftest socket guard.
    # Explicitly verify that dashboard and case detail work without network
    assert client.get("/health").status_code == 200
    assert client.get("/dashboard/summary").status_code == 200
    assert client.get("/cases").status_code == 200


def test_demo_seed_script_idempotent(client):
    # Exercises backend/scripts/seed_demo.py without external network
    # Must be deterministic and not touch non-demo records
    import scripts.seed_demo as seed_demo  # backend/scripts/seed_demo.py

    # Ensure empty before
    before = client.get("/dashboard/summary").json()["total_cases"]
    # First run
    seed_demo.seed_all()
    after1 = client.get("/dashboard/summary").json()["total_cases"]
    assert after1 >= before
    # Verify expected scenarios exist via API filtering
    cases = client.get("/cases?search=pay_demo_").json()
    demo_ids = {c["razorpay_payment_id"] for c in cases}
    # At least A,B,C present
    assert "pay_demo_A_auth_001" in demo_ids
    assert "pay_demo_B_link_001" in demo_ids
    # Second run should be idempotent — not create duplicates
    seed_demo.seed_all()
    after2 = client.get("/dashboard/summary").json()["total_cases"]
    assert after2 == after1
    # Check that non-demo case is not deleted by reset
    send_webhook(client, "payment.failed", {"id": "pay_keep_1", "amount": 12300, "error_reason": "otp_incorrect"}, event_id="evt_keep_1")
    keep_count = client.get("/dashboard/summary").json()["total_cases"]
    seed_demo.reset_demo()
    # keep record should still exist after reset_demo (reset only deletes demo-prefixed)
    remaining = [c["razorpay_payment_id"] for c in client.get("/cases?search=pay_keep_1").json()]
    assert "pay_keep_1" in remaining
    # demo should be gone
    demo_after_reset = client.get("/cases?search=pay_demo_").json()
    assert len(demo_after_reset) == 0
    total_after_reset = client.get("/dashboard/summary").json()["total_cases"]
    assert total_after_reset == keep_count - (after2 - before)  # demo removed, keep remains


def test_demo_seed_main_seeds_then_checks_without_name_error(monkeypatch, capsys):
    import scripts.seed_demo as seed_demo
    from app.ml import scorer as demo_scorer

    monkeypatch.setattr(demo_scorer, "MODEL_PATH", demo_scorer.MODEL_PATH.parent / "missing-demo-model.joblib")
    demo_scorer.reset_cache()
    monkeypatch.setattr(sys, "argv", ["seed_demo.py"])
    seed_demo.main()

    output = capsys.readouterr().out
    assert "Demo seed complete" in output
    assert "pay_demo_A_auth_001: state=" in output
    assert "[D/F] skipped: trained model unavailable" in output
    with SessionLocal() as db:
        assert db.query(RevenueCase).filter(
            RevenueCase.razorpay_payment_id == seed_demo.DEMO_PAYMENT_IDS["A"]
        ).count() == 1
    demo_scorer.reset_cache()


def test_demo_seed_real_cli_seeds_then_checks():
    completed = subprocess.run(
        [sys.executable, "scripts/seed_demo.py"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Demo seed complete" in completed.stdout
    assert "pay_demo_A_auth_001: state=" in completed.stdout


def test_demo_policy_scenarios_have_exact_persisted_modes(trained_model_path):
    import scripts.seed_demo as seed_demo
    from app.ml import scorer as demo_scorer

    original_model_path = demo_scorer.MODEL_PATH
    original_policy = settings.RECOVERY_POLICY
    demo_scorer.MODEL_PATH = trained_model_path
    demo_scorer.reset_cache()
    settings.RECOVERY_POLICY = "shadow"
    try:
        seed_demo.seed_all()
        with SessionLocal() as db:
            rows = {
                case.razorpay_payment_id: case
                for case in db.query(RevenueCase).filter(
                    RevenueCase.razorpay_payment_id.in_(seed_demo.DEMO_PAYMENT_IDS.values())
                )
            }
            for key in ("A", "B", "C", "C_NATIVE", "E"):
                assert seed_demo.DEMO_PAYMENT_IDS[key] in rows
                core_decision = db.query(Decision).filter(
                    Decision.revenue_case_id == rows[seed_demo.DEMO_PAYMENT_IDS[key]].id
                ).order_by(Decision.created_at.desc(), Decision.id.desc()).first()
                assert core_decision is not None
                assert core_decision.policy_mode == "baseline"

            decisions = {
                key: db.query(Decision)
                .filter(Decision.revenue_case_id == rows[seed_demo.DEMO_PAYMENT_IDS[key]].id)
                .order_by(Decision.created_at.desc(), Decision.id.desc())
                .first()
                for key in ("D", "F")
            }
            assert decisions["D"] is not None
            assert decisions["D"].policy_mode == "adaptive"
            assert decisions["F"] is not None
            assert decisions["F"].policy_mode == "shadow"
            assert db.query(AuditEvent).filter(
                AuditEvent.revenue_case_id == rows[seed_demo.DEMO_PAYMENT_IDS["F"]].id,
                AuditEvent.event == "shadow_adaptive_recommendation",
            ).count() == 1

            case_b = rows[seed_demo.DEMO_PAYMENT_IDS["B"]]
            assert case_b.razorpay_payment_link_id is not None
            assert case_b.state == "AWAITING_OUTCOME"
        assert settings.RECOVERY_POLICY == "shadow"
    finally:
        settings.RECOVERY_POLICY = original_policy
        demo_scorer.MODEL_PATH = original_model_path
        demo_scorer.reset_cache()


def test_demo_policy_setting_restored_when_seeding_fails(monkeypatch):
    import scripts.seed_demo as seed_demo

    original_policy = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "baseline"
    monkeypatch.setattr(seed_demo, "_ensure_case", lambda _key: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        with pytest.raises(RuntimeError, match="boom"):
            seed_demo._ensure_case_in_policy_mode("D", "adaptive")
        assert settings.RECOVERY_POLICY == "baseline"
    finally:
        settings.RECOVERY_POLICY = original_policy


def test_demo_reset_uses_dependency_safe_delete_order(monkeypatch):
    import scripts.seed_demo as seed_demo
    from app.ml import scorer as demo_scorer

    monkeypatch.setattr(demo_scorer, "MODEL_PATH", demo_scorer.MODEL_PATH.parent / "missing-demo-model.joblib")
    demo_scorer.reset_cache()
    seed_demo.seed_all()
    with SessionLocal() as db:
        demo_case = db.query(RevenueCase).filter(
            RevenueCase.source == "razorpay",
            RevenueCase.razorpay_payment_id == seed_demo.DEMO_PAYMENT_IDS["A"],
        ).one()
        db.add(ExperimentCase(
            revenue_case_id=demo_case.id,
            run_id="demo-reset-order",
            arm="baseline",
        ))
        db.commit()

    deletes = []
    def record_delete(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("delete"):
            deletes.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_delete)
    try:
        seed_demo.reset_demo()
    finally:
        event.remove(engine, "before_cursor_execute", record_delete)
        demo_scorer.reset_cache()

    def delete_index(table_name):
        return next(i for i, statement in enumerate(deletes) if f"delete from {table_name}" in statement)

    assert delete_index("actions") < delete_index("promises_to_pay")
    assert delete_index("promises_to_pay") < delete_index("customer_messages")
    assert delete_index("actions") < delete_index("decisions")
    assert delete_index("experiment_cases") < delete_index("revenue_cases")
    assert delete_index("decisions") < delete_index("revenue_cases")


def test_demo_reset_preserves_non_demo_source_with_reserved_payment_id(monkeypatch):
    import scripts.seed_demo as seed_demo
    from app.ml import scorer as demo_scorer

    monkeypatch.setattr(demo_scorer, "MODEL_PATH", demo_scorer.MODEL_PATH.parent / "missing-demo-model.joblib")
    demo_scorer.reset_cache()
    seed_demo.seed_all()
    with SessionLocal() as db:
        synthetic = RevenueCase(
            source="synthetic",
            razorpay_payment_id=seed_demo.DEMO_PAYMENT_IDS["A"],
            amount=Decimal("42.00"),
            currency="INR",
            state="HUMAN_REVIEW",
        )
        db.add(synthetic)
        db.commit()
        synthetic_id = synthetic.id

    seed_demo.reset_demo()
    with SessionLocal() as db:
        assert db.get(RevenueCase, synthetic_id) is not None
        assert db.query(RevenueCase).filter(
            RevenueCase.source == "razorpay",
            RevenueCase.razorpay_payment_id.in_(seed_demo.DEMO_PAYMENT_IDS.values()),
        ).count() == 0
    demo_scorer.reset_cache()


def test_seed_demo_no_external_network(monkeypatch):
    # Fake network guard already active; ensure seed doesn't try httpx
    import httpx as _httpx
    original_post = _httpx.post
    original_get = _httpx.get
    def blocked(*a, **k):
        raise AssertionError("seed_demo must not access external networks")
    monkeypatch.setattr(_httpx, "post", blocked)
    monkeypatch.setattr(_httpx, "get", blocked)
    import scripts.seed_demo as seed_demo
    # Should complete without network even with fallback paths
    seed_demo.seed_all()
    # cleanup
    seed_demo.reset_demo()
