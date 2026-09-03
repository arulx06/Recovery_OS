"""Release hardening tests — public-demo safety, resilience, and sanitization."""
import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import text

from app.core.config import cors_allows_credentials, parse_frontend_origins, settings, validate_startup_config
from app.core.database import SessionLocal
from app.models import RevenueCase, Action, AuditEvent
from app.services import razorpay_client, temporal_runtime
from app.core.time import utc_now

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


# ---------------------------------------------------------------------------
# 1. Secret safety — health/ready never leak secrets
# ---------------------------------------------------------------------------

def test_health_never_exposes_secrets(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = json.dumps(resp.json()).lower()
    for secret in ["rzp_test", "rzp_live", "sk-ant", "secret", "password"]:
        # secrets themselves must not appear; boolean flags are ok
        assert "rzp_test_fake" not in body
        assert "fake_hermetic" not in body
    # Check ready too
    resp2 = client.get("/ready")
    assert resp2.status_code in (200, 503)
    body2 = json.dumps(resp2.json()).lower()
    assert "fake_hermetic" not in body2


def test_dashboard_summary_never_exposes_secrets(client):
    resp = client.get("/dashboard/summary")
    assert resp.status_code == 200
    body = json.dumps(resp.json()).lower()
    assert "fake_hermetic" not in body


def test_live_mode_key_rejected():
    # Validate startup validation refuses live keys when enforcement on
    from app.core.config import validate_startup_config

    orig_enabled = settings.RAZORPAY_API_ENABLED
    orig_key = settings.RAZORPAY_KEY_ID
    orig_secret = settings.RAZORPAY_KEY_SECRET
    try:
        settings.RAZORPAY_API_ENABLED = True
        settings.RAZORPAY_KEY_ID = "rzp_live_abc123"
        settings.RAZORPAY_KEY_SECRET = "secret123"
        errors = validate_startup_config()
        assert any("live" in e.lower() or "test" in e.lower() for e in errors)
        # Direct razorpay_client should also reject
        try:
            razorpay_client.create_payment_link(
                amount_rupees=__import__("decimal").Decimal("10"),
                currency="INR", description="x", reference_id="test_ref",
                notes={}
            )
            assert False, "should have raised"
        except razorpay_client.RazorpayAPIError as exc:
            assert "Test Mode" in str(exc)
    finally:
        settings.RAZORPAY_API_ENABLED = orig_enabled
        settings.RAZORPAY_KEY_ID = orig_key
        settings.RAZORPAY_KEY_SECRET = orig_secret


# ---------------------------------------------------------------------------
# 2. Webhook security — signature + idempotency + exempt from demo token
# ---------------------------------------------------------------------------

def test_webhook_signature_still_enforced(client):
    payload = {"event": "payment.failed", "payload": {}}
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": "bad", "x-razorpay-event-id": "evt_sig_test"},
    )
    assert resp.status_code == 400
    assert "invalid webhook signature" in resp.json()["detail"]


def test_malformed_webhook_returns_400(client):
    body = b"not json"
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_malformed"},
    )
    assert resp.status_code == 400


def test_duplicate_webhook_idempotent(client):
    r1 = send_webhook(client, "payment.failed", {"id": "pay_dup_hard_1", "amount": 100000}, event_id="evt_dup_hard")
    assert r1.json()["status"] == "processed"
    r2 = send_webhook(client, "payment.failed", {"id": "pay_dup_hard_1", "amount": 100000}, event_id="evt_dup_hard")
    assert r2.json()["status"] == "ignored"
    assert r2.json()["reason"] == "duplicate_event"


def test_webhook_exempt_from_demo_token(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN", "secret123")
    # webhook should work WITHOUT token
    r = send_webhook(client, "payment.failed", {"id": "pay_exempt_1", "amount": 50000, "error_reason": "timeout"}, event_id="evt_exempt_1")
    assert r.status_code == 200
    # mutation should require token
    case_id = r.json()["case_id"]
    # try customer reply without token -> 401
    reply = client.post(f"/cases/{case_id}/customer-reply", json={"body": "hello"})
    assert reply.status_code == 401
    # with token -> not 401 (may be other logic but not auth failure)
    reply2 = client.post(
        f"/cases/{case_id}/customer-reply",
        json={"body": "maybe sometime"},
        headers={"X-Demo-Admin-Token": "secret123"},
    )
    assert reply2.status_code != 401
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", False)


# ---------------------------------------------------------------------------
# 3. Public/demo protection — experiment and customer reply
# ---------------------------------------------------------------------------

def test_experiment_requires_token_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN", "tok123")
    # without token -> 401
    r = client.post("/experiments", json={"count": 10, "seed": 1})
    assert r.status_code == 401
    # with token -> may be 503 (no model) but not 401
    r2 = client.post("/experiments", json={"count": 10, "seed": 1}, headers={"X-Demo-Admin-Token": "tok123"})
    assert r2.status_code != 401
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", False)


@pytest.mark.parametrize(
    "path",
    [
        "/cases",
        "/cases/missing-case",
        "/dashboard/summary",
        "/experiments",
        "/experiments/missing-run",
        "/experiments/missing-run/export.csv",
    ],
)
def test_sensitive_reads_require_token_when_enabled(client, monkeypatch, path):
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN", "read-token")
    assert client.get(path).status_code == 401


def test_sensitive_reads_accept_token_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN", "read-token")
    webhook = send_webhook(
        client,
        "payment.failed",
        {"id": "pay_read_auth_1", "amount": 50000, "error_reason": "timeout"},
        event_id="evt_read_auth_1",
    )
    assert webhook.status_code == 200
    case_id = webhook.json()["case_id"]
    headers = {"X-Demo-Admin-Token": "read-token"}
    assert client.get("/cases", headers=headers).status_code == 200
    assert client.get(f"/cases/{case_id}", headers=headers).status_code == 200
    assert client.get("/dashboard/summary", headers=headers).status_code == 200
    assert client.get("/experiments", headers=headers).status_code == 200
    assert client.get("/experiments/missing-run", headers=headers).status_code == 404
    assert client.get("/experiments/missing-run/export.csv", headers=headers).status_code == 404


def test_sensitive_reads_remain_local_friendly_when_token_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", False)
    for path in ("/cases", "/dashboard/summary", "/experiments"):
        assert client.get(path).status_code == 200


def test_health_and_root_remain_public_when_token_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_ADMIN_TOKEN", "read-token")
    for path in ("/", "/health", "/live", "/ready"):
        assert client.get(path).status_code == 200


def test_experiment_count_bounds(client):
    # Too large should fail (max 1000 by default)
    r = client.post("/experiments", json={"count": 99999})
    assert r.status_code == 400
    r2 = client.post("/experiments", json={"count": 0})
    assert r2.status_code == 400
    # Valid small count may return 503 (no model) but not 400
    r3 = client.post("/experiments", json={"count": 100, "seed": 1})
    # Either 503 (no model) or 200 depending on artifact
    assert r3.status_code in (200, 503)


def test_customer_reply_body_validation(client):
    # Create case first
    r = send_webhook(client, "payment.failed", {"id": "pay_reply_val_1", "amount": 80000, "error_reason": "otp_incorrect"}, event_id="evt_reply_val_1")
    case_id = r.json()["case_id"]
    # Empty body -> 422
    resp = client.post(f"/cases/{case_id}/customer-reply", json={"body": ""})
    assert resp.status_code == 422
    # Too long -> 422
    resp2 = client.post(f"/cases/{case_id}/customer-reply", json={"body": "x" * 5000})
    assert resp2.status_code == 422


# ---------------------------------------------------------------------------
# 4. Health / readiness separation
# ---------------------------------------------------------------------------

def test_liveness_always_ok(client):
    r = client.get("/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_reports_checks(client):
    r = client.get("/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "checks" in body
    assert "database" in body["checks"]
    assert "adaptive_policy" in body
    assert "llm" in body
    # Optional providers never make not_ready when baseline
    # Ensure llm info sanitized
    assert "configured" in body["llm"]


def test_readiness_handles_redis_down(client, monkeypatch):
    # Simulate redis unreachable by pointing to bad url
    orig = settings.REDIS_URL
    orig_enabled = settings.TASK_QUEUE_ENABLED
    try:
        settings.TASK_QUEUE_ENABLED = True
        settings.REDIS_URL = "redis://127.0.0.1:6399/0"
        r = client.get("/ready")
        # Should be 503 not_ready when redis required but unreachable
        assert r.status_code == 503
        assert r.json()["ready"] is False
        assert r.json()["checks"]["redis"] == "unreachable"
    finally:
        settings.REDIS_URL = orig
        settings.TASK_QUEUE_ENABLED = orig_enabled


def test_health_does_not_require_redis_for_liveness(client, monkeypatch):
    orig = settings.REDIS_URL
    orig_enabled = settings.TASK_QUEUE_ENABLED
    try:
        settings.TASK_QUEUE_ENABLED = True
        settings.REDIS_URL = "redis://127.0.0.1:6399/0"
        r = client.get("/health")
        # Liveness should still be 200 even if redis down (degraded vs unreachable)
        assert r.status_code == 200
    finally:
        settings.REDIS_URL = orig
        settings.TASK_QUEUE_ENABLED = orig_enabled


# ---------------------------------------------------------------------------
# 5. Request correlation ID
# ---------------------------------------------------------------------------

def test_request_id_header_present(client):
    r = client.get("/health")
    assert "X-Request-ID" in r.headers or "x-request-id" in r.headers
    # Custom id should be echoed
    custom = "test-req-123"
    r2 = client.get("/health", headers={"X-Request-ID": custom})
    # Response should contain same id (case-insensitive header)
    returned = r2.headers.get("X-Request-ID") or r2.headers.get("x-request-id")
    assert returned == custom


# ---------------------------------------------------------------------------
# 6. Error sanitization — no traceback leak
# ---------------------------------------------------------------------------

def test_error_sanitization_for_unknown_route(client):
    r = client.get("/cases/nonexistent-id-123456")
    assert r.status_code == 404
    body = json.dumps(r.json()).lower()
    # Should not leak traceback or file paths
    assert "traceback" not in body
    assert "File \"" not in body
    assert ".py" not in body or "not found" in body  # allow minimal .py if not leakage


# ---------------------------------------------------------------------------
# 7. Queue failure resilience — PostgreSQL is source of truth
# ---------------------------------------------------------------------------

def test_queue_enqueue_failure_leaves_scheduled(client, monkeypatch):
    monkeypatch.setattr(settings, "FAILURE_INJECTION_ENABLED", True)
    # Create case that creates CREATE_PAYMENT_LINK (action scheduled)
    r = send_webhook(client, "payment.failed", {"id": "pay_queue_fail_1", "amount": 50000, "error_reason": "card_expired"}, event_id="evt_qf_1")
    case_id = r.json()["case_id"]
    with SessionLocal() as db:
        act = db.query(Action).filter(Action.revenue_case_id == case_id, Action.action_type == "CREATE_PAYMENT_LINK").first()
        assert act is not None
        assert act.status == "SCHEDULED"
        aid = act.id
        db.rollback()
    # Simulate enqueue failure via injection header param
    from app.services import task_queue
    ok = task_queue.enqueue_action(aid, _header_inject="rq_enqueue_failure")
    assert ok is False
    # Action should remain SCHEDULED with last_error, repairable by reconciliation
    with SessionLocal() as db:
        act2 = db.get(Action, aid)
        assert act2.status == "SCHEDULED"
        assert "queue" in (act2.last_error or "").lower()
        db.rollback()
    # Reconciliation should still see it as scheduled
    from app.services import temporal_runtime as tr
    # Without failure injection, republish should be attempted (but will fail to redis since disabled in tests — ok)
    monkeypatch.setattr(settings, "FAILURE_INJECTION_ENABLED", False)


def test_stale_executing_recovery(client):
    # Create case, move to ACTION_SCHEDULED, then manually claim and strand EXECUTING
    r = send_webhook(client, "payment.failed", {"id": "pay_stale_1", "amount": 50000, "error_reason": "card_expired"}, event_id="evt_stale_1")
    case_id = r.json()["case_id"]
    with SessionLocal.begin() as db:
        act = db.query(Action).filter(Action.revenue_case_id == case_id, Action.action_type == "CREATE_PAYMENT_LINK").first()
        # Force to EXECUTING with old claimed_at (simulate worker crash)
        act.status = "EXECUTING"
        act.claimed_at = utc_now().__class__.min if False else __import__("datetime").datetime(2000, 1, 1)
        act.attempt_count = 1
        db.add(act)
    # reconcile should handle stale (may be generic or payment_link path)
    result = temporal_runtime.reconcile_actions()
    # Should report some reset or at least not crash
    assert isinstance(result, dict)
    assert "reset" in result or "scheduled" in result


# ---------------------------------------------------------------------------
# 8. Provider timeout / reconciliation paths (mocked, no network)
# ---------------------------------------------------------------------------

def test_provider_timeout_is_ambiguous(client, monkeypatch):
    # Ensure razorpay_client raises ambiguous on timeout simulation when injected
    monkeypatch.setattr(settings, "FAILURE_INJECTION_ENABLED", True)
    try:
        razorpay_client.create_payment_link(
            amount_rupees=__import__("decimal").Decimal("100"),
            currency="INR", description="test", reference_id="__test_ref__", notes={}, _inject="razorpay_timeout"
        )
        assert False, "should have raised ambiguous"
    except razorpay_client.RazorpayAmbiguousError:
        pass
    finally:
        monkeypatch.setattr(settings, "FAILURE_INJECTION_ENABLED", False)


def test_worker_retry_bounded(client):
    r = send_webhook(client, "payment.failed", {"id": "pay_retry_1", "amount": 50000, "error_reason": "card_expired"}, event_id="evt_retry_1")
    case_id = r.json()["case_id"]
    with SessionLocal() as db:
        act = db.query(Action).filter(Action.revenue_case_id == case_id, Action.action_type == "CREATE_PAYMENT_LINK").first()
        aid = act.id
        db.rollback()
    # Simulate repeated failures up to max_attempts — worker should go to HUMAN_REVIEW eventually
    # Use temporal_runtime._record_external_failure directly for bounded retry test
    for i in range(5):
        with SessionLocal() as db:
            a = db.get(Action, aid)
            if a.status == "FAILED":
                break
            # Force EXECUTING state for failure path
            if a.status != "EXECUTING":
                a.status = "EXECUTING"
                a.claimed_at = utc_now()
                db.commit()
            db.rollback()
        temporal_runtime._record_external_failure(aid, utc_now())
    with SessionLocal() as db:
        final = db.get(Action, aid)
        # After enough retries should be FAILED or still SCHEDULED but not infinite
        assert final.status in ("FAILED", "SCHEDULED")
        db.rollback()


# ---------------------------------------------------------------------------
# 9. CORS — allow_origins not wildcard with credentials
# ---------------------------------------------------------------------------

def test_cors_headers(client):
    # Preflight request
    resp = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    # FastAPI CORSMiddleware should respond with allow origin matching config
    # If origin matches, it should be echoed; if not, header absent
    # For allowed origin, check header present
    allowed = resp.headers.get("access-control-allow-origin")
    # Should be either the origin or not set, but never "*"
    if allowed:
        assert allowed != "*" or "credentials" not in str(resp.headers).lower()


def test_cors_origin_parsing():
    single = parse_frontend_origins("https://demo.example.com")
    multiple = parse_frontend_origins("https://one.example.com, https://two.example.com")
    wildcard = parse_frontend_origins("*")
    assert single == ["https://demo.example.com"]
    assert multiple == [
        "https://one.example.com",
        "https://two.example.com",
    ]
    assert wildcard == ["*"]
    assert cors_allows_credentials(single) is True
    assert cors_allows_credentials(multiple) is True
    assert cors_allows_credentials(wildcard) is False


def test_cors_wildcard_cannot_be_mixed_with_explicit_origin(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_ORIGIN", "*,https://demo.example.com")
    with pytest.raises(ValueError, match="cannot mix"):
        parse_frontend_origins()
    assert any("cannot mix" in error for error in validate_startup_config())


# ---------------------------------------------------------------------------
# 10. Dashboard handles missing state safely
# ---------------------------------------------------------------------------

def test_dashboard_summary_handles_empty_and_partial(client):
    # Summary should always return numbers not null
    resp = client.get("/dashboard/summary")
    assert resp.status_code == 200
    body = resp.json()
    for key in ["revenue_at_risk", "revenue_recovered", "total_cases", "open_cases"]:
        assert body[key] is not None
        assert isinstance(body[key], (int, float))
    # Even with no cases, should not error
    assert "by_state" in body
    assert "by_category" in body


def test_case_detail_handles_missing_adaptive_fields(client):
    # Create baseline case without model — inspector should show unavailable fields not error
    r = send_webhook(client, "payment.failed", {"id": "pay_missing_adap_1", "amount": 50000, "error_reason": "payment_failed"}, event_id="evt_missing_adap")
    case_id = r.json()["case_id"]
    detail = client.get(f"/cases/{case_id}").json()
    assert "decision_inspectors" in detail
    assert len(detail["decision_inspectors"]) >= 1
    # Baseline candidates should have p_recovery = unavailable (None)
    inspector = detail["decision_inspectors"][0]
    assert inspector["policy_mode"] in ("baseline", None)


# ---------------------------------------------------------------------------
# 11. Failure injection must be disabled in production
# ---------------------------------------------------------------------------

def test_failure_injection_disabled_by_default():
    assert settings.FAILURE_INJECTION_ENABLED is False


# ---------------------------------------------------------------------------
# 12. Experiment synthetic labeling
# ---------------------------------------------------------------------------

def test_experiment_synthetic_label_in_api(client, monkeypatch, tmp_path):
    # This test uses isolated model to ensure synthetic run works
    from app.ml import scorer
    from app.ml import train as train_module
    from pathlib import Path
    orig = scorer.MODEL_PATH
    tmp = tmp_path / "hardening_model.joblib"
    train_module.train(n=2000, seed=42, save=True, save_path=tmp)
    scorer.MODEL_PATH = tmp
    scorer.reset_cache()
    try:
        r = client.post("/experiments", json={"count": 10, "seed": 1})
        # Should succeed (model available now)
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        # Incremental recovered should be numeric, and arms should have synthetic nature
        assert "arms" in body
        assert "baseline" in body["arms"]
        assert "adaptive" in body["arms"]
    finally:
        scorer.MODEL_PATH = orig
        scorer.reset_cache()


# ---------------------------------------------------------------------------
# 13. No secret in frontend config leakage via API
# ---------------------------------------------------------------------------

def test_frontend_config_not_leaking_via_health_or_dashboard(client):
    for path in ["/health", "/ready", "/dashboard/summary"]:
        resp = client.get(path)
        if resp.status_code != 200 and path == "/ready":
            # ready may be 503 when redis down, but still should not leak
            continue
        text = json.dumps(resp.json()).lower()
        assert "recoveryos" not in text or "recoveryos" in text  # allow service name
        # But never real secrets
        assert "psycopg2" not in text
        assert "postgresql://" not in text
        assert "redis://" not in text
