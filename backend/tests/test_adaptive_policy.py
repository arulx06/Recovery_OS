from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Action, AuditEvent, RevenueCase, Decision
from app.services import policy_engine, ml_policy, policy_dispatcher
from app.ml import scorer, friction
from app.ml.friction import BASE_FRICTION, PROFILE_WEIGHTS


NOW = datetime(2026, 9, 3, 10, 0, 0)


def make_case(db, **overrides):
    defaults = dict(source="razorpay", amount=Decimal("1000"), currency="INR", failure_category="INVALID_INSTRUMENT", state="DIAGNOSED")
    defaults.update(overrides)
    case = RevenueCase(**defaults)
    db.add(case)
    db.flush()
    return case


@pytest.fixture(scope="module", autouse=True)
def trained_model(trained_model_path):
    original = scorer.MODEL_PATH
    scorer.MODEL_PATH = trained_model_path
    scorer.reset_cache()
    yield
    scorer.reset_cache()
    scorer.MODEL_PATH = original


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


# Baseline default
def test_baseline_is_default(db):
    assert (settings.RECOVERY_POLICY or "baseline").lower() == "baseline"


def test_baseline_never_loads_model_unnecessarily(monkeypatch, db):
    # In baseline mode, decide_for_case should not call scorer._load
    original_load = scorer._load
    called = {"n": 0}
    def failing_load(*a, **k):
        called["n"] += 1
        return original_load(*a, **k)
    monkeypatch.setattr(scorer, "_load", failing_load)
    case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE")
    # Ensure baseline mode
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "baseline"
    try:
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision is not None
        # baseline should not have loaded model
        assert called["n"] == 0
    finally:
        settings.RECOVERY_POLICY = old


def test_shadow_executes_baseline_and_audits_recommendation(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "shadow"
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT", amount=Decimal("2000"))
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision is not None
        # Should be baseline action for invalid instrument (CREATE_PAYMENT_LINK)
        assert decision.policy_mode == "shadow"
        # Check shadow audit exists
        audit = db.query(AuditEvent).filter(AuditEvent.event == "shadow_adaptive_recommendation").first()
        assert audit is not None
        assert "baseline_chosen" in audit.detail
        assert "adaptive_suggested" in audit.detail
        # No second Action
        actions = db.query(Action).filter(Action.revenue_case_id == case.id).all()
        assert len(actions) == 1  # only baseline's action
    finally:
        settings.RECOVERY_POLICY = old


def test_shadow_has_no_side_effects(db, monkeypatch):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "shadow"
    # Ensure adaptive would suggest something but not create action
    case = make_case(db, failure_category="CUSTOMER_AUTHENTICATION", amount=Decimal("1500"))
    # Mock create_payment_link to ensure not called in shadow
    from app.services import razorpay_client
    def fail_create(*a, **k):
        raise AssertionError("shadow should not call provider")
    monkeypatch.setattr(razorpay_client, "create_payment_link", fail_create)
    try:
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision.policy_mode == "shadow"
        assert db.query(Action).filter(Action.revenue_case_id == case.id).count() == 1
    finally:
        settings.RECOVERY_POLICY = old


def test_adaptive_uses_model_when_healthy(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    settings.ADAPTIVE_POLICY_PROFILE = "balanced"
    settings.ADAPTIVE_FRICTION_WEIGHT = None
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT", amount=Decimal("3000"))
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision is not None
        assert decision.policy_mode == "adaptive"
        assert decision.model_version is not None
        assert decision.model_fingerprint is not None
        assert decision.friction_profile == "balanced"
        # Check candidates have utility
        assert "_provenance" in decision.alternatives
        # At least one candidate has utility
        candidates = {k: v for k, v in decision.alternatives.items() if not k.startswith("_")}
        assert any("utility" in v for v in candidates.values() if v.get("allowed"))
    finally:
        settings.RECOVERY_POLICY = old
        settings.ADAPTIVE_POLICY_PROFILE = "balanced"
        settings.ADAPTIVE_FRICTION_WEIGHT = None


def test_adaptive_fallback_on_missing_model(db, monkeypatch):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    original = scorer.MODEL_PATH
    scorer.MODEL_PATH = original.parent / "missing_model.joblib"
    scorer.reset_cache()
    try:
        case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE")
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision is not None
        assert decision.policy_mode == "adaptive_fallback"
        assert db.query(AuditEvent).filter(AuditEvent.event == "adaptive_fallback").first() is not None
        assert case.state != "DIAGNOSED"
        assert case.state != "DECISION_READY"
    finally:
        scorer.MODEL_PATH = original
        scorer.reset_cache()
        settings.RECOVERY_POLICY = old


def test_adaptive_cannot_bypass_max_contacts(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="CUSTOMER_AUTHENTICATION", amount=Decimal("500"))
        # Seed max contacts
        with SessionLocal() as s:
            c = s.get(RevenueCase, case.id)
            # need to commit contacts via db
            pass
        # Add contacts directly via db
        for i in range(3):
            db.add(Action(revenue_case_id=case.id, action_type="CONTACT_CUSTOMER", status="EXECUTED", scheduled_for=NOW))
        db.flush()
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision.chosen_action != "CONTACT_CUSTOMER"
        assert decision.chosen_action not in policy_engine.CONTACT_ACTIONS or False
    finally:
        settings.RECOVERY_POLICY = old


def test_adaptive_cannot_bypass_cooldown(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="CUSTOMER_AUTHENTICATION", amount=Decimal("500"))
        db.add(Action(revenue_case_id=case.id, action_type="CONTACT_CUSTOMER", status="EXECUTED", scheduled_for=NOW, created_at=NOW))
        db.flush()
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        # Cooldown 12h should block immediate second contact
        assert decision.chosen_action != "CONTACT_CUSTOMER"
    finally:
        settings.RECOVERY_POLICY = old


def test_adaptive_cannot_bypass_max_amount(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT", amount=Decimal("99999"))
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision.chosen_action not in policy_engine.CONTACT_ACTIONS
    finally:
        settings.RECOVERY_POLICY = old


def test_adaptive_cannot_bypass_stopping_rule(db):
    from app.models import AuditEvent as AE
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE")
        for _ in range(6):
            db.add(AE(revenue_case_id=case.id, event="failure_event_received", detail={}))
        db.flush()
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision.chosen_action == "STOP"
        assert decision.policy_mode in ("adaptive", "adaptive_fallback", "baseline")
    finally:
        settings.RECOVERY_POLICY = old


def test_adaptive_cannot_select_invalid_native_retry(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT", amount=Decimal("1000"))
        # INVALID_INSTRUMENT is not subscription, so WAIT_FOR_NATIVE_RETRY is semantically invalid
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision.chosen_action != "WAIT_FOR_NATIVE_RETRY"
    finally:
        settings.RECOVERY_POLICY = old


def test_adaptive_does_not_directly_execute(db, monkeypatch):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    from app.services import razorpay_client
    def fail_create(*a, **k):
        raise AssertionError("adaptive should not directly call provider; via worker only")
    monkeypatch.setattr(razorpay_client, "create_payment_link", fail_create)
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT", amount=Decimal("1200"))
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        # Should have created a SCHEDULED action, not EXECUTED
        action = db.query(Action).filter(Action.decision_id == decision.id).first()
        assert action.status == "SCHEDULED"
    finally:
        settings.RECOVERY_POLICY = old


def test_friction_wait_lower_than_contact(db):
    case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE")
    wait_friction = friction.compute_friction_score("WAIT", case, db)
    contact_friction = friction.compute_friction_score("CONTACT_CUSTOMER", case, db)
    assert wait_friction < contact_friction


def test_friction_repeated_contacts_increase(db):
    case = make_case(db, failure_category="CUSTOMER_AUTHENTICATION")
    f1 = friction.compute_friction_score("CONTACT_CUSTOMER", case, db)
    db.add(Action(revenue_case_id=case.id, action_type="CONTACT_CUSTOMER", status="EXECUTED", scheduled_for=NOW))
    db.flush()
    f2 = friction.compute_friction_score("CONTACT_CUSTOMER", case, db)
    assert f2 > f1


def test_friction_profile_effect(db):
    from app.ml.friction import compute_utility, get_friction_weight
    # Profile resolution must be exact and deterministic
    assert get_friction_weight("revenue_first", None) == ("revenue_first", 4.0)
    assert get_friction_weight("balanced", None) == ("balanced", 18.0)
    assert get_friction_weight("low_friction", None) == ("low_friction", 45.0)
    assert get_friction_weight("balanced", 99.0) == ("custom:99.0", 99.0)
    assert get_friction_weight("unknown_profile", None) == ("balanced", 18.0)
    # With identical p/amount/cost, higher friction weight penalizes high-friction action more
    wait_utility_low_weight = compute_utility(0.5, 2000, 0, 0, 4.0)
    contact_utility_low_weight = compute_utility(0.5, 2000, 5, 25, 4.0)
    wait_utility_high_weight = compute_utility(0.5, 2000, 0, 0, 45.0)
    contact_utility_high_weight = compute_utility(0.5, 2000, 5, 25, 45.0)
    # Same p, so friction difference decides: low weight slightly penalizes, high weight heavily
    assert (wait_utility_low_weight - contact_utility_low_weight) < (wait_utility_high_weight - contact_utility_high_weight)
    assert contact_utility_high_weight < wait_utility_high_weight  # at high weight, WAIT wins
    # Concrete scenario where revenue_first selects CONTACT but low_friction selects WAIT
    # Use scorer ranking to prove profile changes decision
    from app.ml.friction import BASE_FRICTION
    context = {"category": "INVALID_INSTRUMENT", "amount": 2000, "days_overdue": 0, "previous_contacts": 0, "subscription_linked": False, "hour": 12, "day_of_week": 2}
    # With revenue_first, contact should outrank wait if its p is moderately higher
    # With low_friction, wait should outrank contact
    # We use mocked p by directly testing utility, then via scorer with real model
    old_profile = settings.ADAPTIVE_POLICY_PROFILE
    old_weight = settings.ADAPTIVE_FRICTION_WEIGHT
    try:
        settings.ADAPTIVE_POLICY_PROFILE = "revenue_first"
        settings.ADAPTIVE_FRICTION_WEIGHT = None
        # Create a case where p difference is small but friction matters
        case_rf = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE", amount=Decimal("2000"))
        # Force a scenario where WAIT and CONTACT have close p
        # We can't guarantee model p, but we can test that low_friction profile is deterministic
        settings.ADAPTIVE_POLICY_PROFILE = "balanced"
        case_bal1 = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE", amount=Decimal("2000"))
        disp1 = policy_dispatcher.decide_for_case(db, case_bal1, now=NOW)
        settings.ADAPTIVE_POLICY_PROFILE = "balanced"
        case_bal2 = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE", amount=Decimal("2000"))
        # Use same seed? For determinism, we check that two identical cases with same profile give same decision
        # This is ensured by deterministic model and friction
        disp2 = policy_dispatcher.decide_for_case(db, case_bal2, now=NOW)
        assert disp1.chosen_action == disp2.chosen_action
    finally:
        settings.ADAPTIVE_POLICY_PROFILE = old_profile
        settings.ADAPTIVE_FRICTION_WEIGHT = old_weight


def test_utility_exposes_components(db):
    # Ensure rank_actions_with_friction returns expected fields
    from app.ml.features import build_live_features
    case = make_case(db, failure_category="CUSTOMER_AUTHENTICATION", amount=Decimal("5000"))
    context = {"category": case.failure_category, "amount": float(case.amount), "days_overdue": 0, "previous_contacts": 0, "subscription_linked": False, "hour": 12, "day_of_week": 2}
    friction_scores = {a: friction.compute_friction_score(a, case, db) for a in ["WAIT", "CONTACT_CUSTOMER"]}
    ranked = scorer.rank_actions_with_friction(context, ["WAIT", "CONTACT_CUSTOMER"], friction_scores, 1.0)
    for r in ranked:
        assert "p_recovery" in r
        assert "expected_value" in r
        assert "expected_recovered_value" in r
        assert "friction_score" in r
        assert "utility" in r


def test_provenance_persisted(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT")
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision.policy_mode == "adaptive"
        assert decision.model_version == "recovery-v1"
        assert decision.model_fingerprint is not None
        assert len(decision.model_fingerprint) == 8 or len(decision.model_fingerprint) == 64
        assert decision.friction_profile == "balanced"
        assert "_provenance" in decision.alternatives
        prov = decision.alternatives["_provenance"]
        assert prov["policy_mode"] == "adaptive"
        assert "fingerprint" in prov
    finally:
        settings.RECOVERY_POLICY = old


def test_case_api_exposes_provenance(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT")
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        db.commit()
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        resp = client.get(f"/cases/{case.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["decisions"][0]["policy_mode"] == "adaptive"
        assert body["decisions"][0]["model_version"] is not None
    finally:
        settings.RECOVERY_POLICY = old


def test_health_exposes_adaptive_info():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "adaptive_policy" in data
    assert "configured_mode" in data["adaptive_policy"]
    assert "model_available" in data["adaptive_policy"]


def test_wait_wake_reenters_dispatcher(monkeypatch, db):
    # Simulate WAIT completion that triggers re-decide via dispatcher
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE", state="WAITING", amount=Decimal("1000"))
        # Create a WAIT action that is EXECUTING
        action = Action(revenue_case_id=case.id, action_type="WAIT", status="EXECUTING", scheduled_for=NOW, claimed_at=NOW, attempt_count=1)
        db.add(action)
        db.flush()
        action_id = action.id
        case_id = case.id
        db.commit()
        # Now complete_wait will set DIAGNOSED and call dispatcher
        from app.services import temporal_runtime
        result = temporal_runtime._complete_wait(action_id, NOW)
        assert result == "executed"
        # After wait, case should have a new decision via adaptive or fallback
        with SessionLocal() as s:
            c = s.get(RevenueCase, case_id)
            assert c.state in ("WAITING", "ACTION_SCHEDULED", "HUMAN_REVIEW", "STOPPED")
            decisions = s.query(Decision).filter(Decision.revenue_case_id == case_id).all()
            assert len(decisions) >= 1
    finally:
        settings.RECOVERY_POLICY = old


def test_repeated_failure_reenters_dispatcher(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        from app.services import orchestrator
        from app.models import PaymentEvent
        case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE", state="WAITING", amount=Decimal("1000"))
        case.razorpay_payment_id = "pay_repeat_test"
        db.flush()
        event = PaymentEvent(razorpay_event_id="evt_repeat_1", event_type="payment.failed")
        db.add(event)
        db.flush()
        payload = {"payload": {"payment": {"entity": {"entity": "payment", "id": "pay_repeat_test", "amount": 100000, "error_reason": "card_expired"}}}}
        updated = orchestrator.handle_event(db, event, "payment.failed", payload)
        # Should have re-diagnosed and created new decision via adaptive
        assert updated.state in ("ACTION_SCHEDULED", "WAITING", "HUMAN_REVIEW", "STOPPED")
        decisions = db.query(Decision).filter(Decision.revenue_case_id == updated.id).all()
        assert len(decisions) >= 1
    finally:
        settings.RECOVERY_POLICY = old


def test_terminal_case_never_gets_adaptive_action(db):
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="INVALID_INSTRUMENT", state="RECOVERED")
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision is None
        assert case.state == "RECOVERED"
    finally:
        settings.RECOVERY_POLICY = old


def test_train_serve_parity():
    # Synthetic training row -> canonical feature builder -> same schema as live
    from app.ml.features import FEATURE_COLUMNS, build_live_features
    from app.ml.train import FEATURE_COLUMNS as TRAIN_COLS
    assert FEATURE_COLUMNS == TRAIN_COLS
    # Ensure feature order validated
    from app.ml.features import FEATURE_SCHEMA_VERSION
    assert FEATURE_SCHEMA_VERSION == "v1"


def test_shadow_pre_decision_no_contamination(db):
    """Regression: baseline Action must not be counted as previous_contacts for shadow."""
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "shadow"
    try:
        # Use INVALID_INSTRUMENT where baseline chooses CREATE_PAYMENT_LINK (a contact action)
        case = make_case(db, failure_category="INVALID_INSTRUMENT", amount=Decimal("2000"))
        # Ensure 0 prior contacts
        assert len([a for a in db.query(Action).filter(Action.revenue_case_id == case.id).all()]) == 0
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision.policy_mode == "shadow"
        # Baseline created exactly one Action
        actions = db.query(Action).filter(Action.revenue_case_id == case.id).all()
        assert len(actions) == 1
        assert actions[0].action_type == "CREATE_PAYMENT_LINK"
        # Shadow audit should have been computed BEFORE that Action existed,
        # so its friction for CREATE should be base 25.0 (0 prior contacts), not 37.0
        audit = db.query(AuditEvent).filter(AuditEvent.event == "shadow_adaptive_recommendation").first()
        assert audit is not None
        candidates = audit.detail.get("candidates", {})
        create_entry = candidates.get("CREATE_PAYMENT_LINK")
        assert create_entry is not None
        # If shadow had seen the baseline Action, friction would be 25+12=37
        assert create_entry.get("friction_score") == 25.0, f"shadow contamination: expected 25.0 but got {create_entry.get('friction_score')}"
    finally:
        settings.RECOVERY_POLICY = old


def test_incompatible_artifact_without_predict_proba_falls_back(db, tmp_path, monkeypatch):
    """Production validator must reject pipeline without predict_proba and adaptive must fallback."""
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    original = scorer.MODEL_PATH
    fake_path = tmp_path / "incompatible.joblib"
    import joblib

    # object() has no predict_proba and is picklable
    joblib.dump({"pipeline": object(), "feature_columns": scorer.FEATURE_COLUMNS}, fake_path)
    scorer.MODEL_PATH = fake_path
    scorer.reset_cache()
    try:
        case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE")
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision is not None
        assert decision.policy_mode == "adaptive_fallback"
        assert db.query(AuditEvent).filter(AuditEvent.event == "adaptive_fallback").first() is not None
        assert case.state != "DIAGNOSED"
        assert case.state != "DECISION_READY"
    finally:
        scorer.MODEL_PATH = original
        scorer.reset_cache()
        settings.RECOVERY_POLICY = old


def test_friction_profile_flip_revenue_vs_low(db, monkeypatch):
    """Concrete: same context/probabilities, revenue_first picks CONTACT, low_friction picks WAIT."""
    # Mock model to give WAIT 0.50, CONTACT 0.70 — enough edge for CONTACT to win at low weight but lose at high
    class FakePipeline:
        def predict_proba(self, rows):
            import numpy as np
            probs = []
            for _, row in rows.iterrows():
                action = row["action_taken"]
                if action == "WAIT":
                    probs.append([0.5, 0.5])
                elif action == "CONTACT_CUSTOMER":
                    probs.append([0.3, 0.7])
                elif action == "CREATE_PAYMENT_LINK":
                    probs.append([0.35, 0.65])
                else:
                    probs.append([0.5, 0.5])
            return np.array(probs)

    fake_bundle = {"pipeline": FakePipeline(), "feature_columns": scorer.FEATURE_COLUMNS, "fingerprint": "fake1234", "fingerprint_short": "fake1234", "model_version": "recovery-v1"}
    monkeypatch.setattr(scorer, "_load", lambda model_path=None: fake_bundle)

    old_profile = settings.ADAPTIVE_POLICY_PROFILE
    old_weight = settings.ADAPTIVE_FRICTION_WEIGHT
    old_policy = settings.RECOVERY_POLICY
    try:
        settings.RECOVERY_POLICY = "adaptive"
        # Revenue-first should pick CONTACT (higher p outweighs small friction)
        settings.ADAPTIVE_POLICY_PROFILE = "revenue_first"
        settings.ADAPTIVE_FRICTION_WEIGHT = None
        case_rf = make_case(db, failure_category="INSUFFICIENT_BALANCE", amount=Decimal("5000"))
        dec_rf = policy_dispatcher.decide_for_case(db, case_rf, now=NOW)
        assert dec_rf.chosen_action == "CONTACT_CUSTOMER", f"revenue_first should pick CONTACT, got {dec_rf.chosen_action}"

        # Low-friction should pick WAIT (friction 40*45=1800 outweighs p edge)
        settings.ADAPTIVE_POLICY_PROFILE = "low_friction"
        case_lf = make_case(db, failure_category="INSUFFICIENT_BALANCE", amount=Decimal("5000"))
        dec_lf = policy_dispatcher.decide_for_case(db, case_lf, now=NOW)
        assert dec_lf.chosen_action == "WAIT", f"low_friction should pick WAIT, got {dec_lf.chosen_action}"

        # Balanced determinism: same case/profile gives same decision
        settings.ADAPTIVE_POLICY_PROFILE = "balanced"
        case_b1 = make_case(db, failure_category="INSUFFICIENT_BALANCE", amount=Decimal("5000"))
        dec_b1 = policy_dispatcher.decide_for_case(db, case_b1, now=NOW)
        case_b2 = make_case(db, failure_category="INSUFFICIENT_BALANCE", amount=Decimal("5000"))
        dec_b2 = policy_dispatcher.decide_for_case(db, case_b2, now=NOW)
        assert dec_b1.chosen_action == dec_b2.chosen_action

        # Direct utility flip with same p/amount but different weights
        from app.ml.friction import compute_utility
        wait_rf = compute_utility(0.50, 5000, 0, 0, 4)
        contact_rf = compute_utility(0.70, 5000, 15, 40, 4)
        assert contact_rf > wait_rf
        wait_lf = compute_utility(0.50, 5000, 0, 0, 45)
        contact_lf = compute_utility(0.70, 5000, 15, 40, 45)
        assert wait_lf > contact_lf

        # Profile resolution and override
        assert friction.get_friction_weight("revenue_first", None) == ("revenue_first", 4.0)
        assert friction.get_friction_weight("balanced", None) == ("balanced", 18.0)
        assert friction.get_friction_weight("low_friction", None) == ("low_friction", 45.0)
        assert friction.get_friction_weight("balanced", 99.0) == ("custom:99.0", 99.0)
    finally:
        settings.ADAPTIVE_POLICY_PROFILE = old_profile
        settings.ADAPTIVE_FRICTION_WEIGHT = old_weight
        settings.RECOVERY_POLICY = old_policy
        scorer.reset_cache()


def test_direct_ml_policy_model_failure_raises(db, monkeypatch):
    """Direct ml_policy call with bad artifact must raise, not silently fallback."""
    original = scorer.MODEL_PATH
    fake_path = db  # dummy, we will monkeypatch load
    def raise_trained(*a, **k):
        raise scorer.ModelNotTrainedError("forced model failure")
    monkeypatch.setattr(scorer, "_load", raise_trained)
    case = make_case(db, failure_category="INVALID_INSTRUMENT")
    with pytest.raises(scorer.ModelNotTrainedError):
        ml_policy.decide_ml(db, case, now=NOW)
    # Case should have been restored to DIAGNOSED, not stuck in DECISION_READY
    assert case.state == "DIAGNOSED"


def test_db_error_not_swallowed_as_fallback(db, monkeypatch):
    """DB persistence errors must not be converted to adaptive_fallback."""
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    # Make policy_engine.record_decision raise a DB-like error
    def raise_db(*a, **k):
        raise RuntimeError("simulated DB flush failure")
    monkeypatch.setattr(policy_engine, "record_decision", raise_db)
    case = make_case(db, failure_category="INVALID_INSTRUMENT")
    with pytest.raises(RuntimeError, match="simulated DB flush failure"):
        policy_dispatcher.decide_for_case(db, case, now=NOW)
    # No fallback audit should have been created for DB error
    assert db.query(AuditEvent).filter(AuditEvent.event == "adaptive_fallback").first() is None
    settings.RECOVERY_POLICY = old


def test_experiment_baseline_invariant_across_profiles(db):
    """Same seed/count must give identical baseline regardless of adaptive profile."""
    from app.services import experiment_runner
    old_profile = settings.ADAPTIVE_POLICY_PROFILE
    old_weight = settings.ADAPTIVE_FRICTION_WEIGHT
    try:
        # Use count 50 for speed
        for profile in ["revenue_first", "balanced", "low_friction"]:
            settings.ADAPTIVE_POLICY_PROFILE = profile
            settings.ADAPTIVE_FRICTION_WEIGHT = None
            # need fresh DB for each profile but same seed
            # Use experiment_runner with same seed=123 and count=50, check baseline metrics
            # We do this via direct calls with same seed and count, but we need to compare
            pass
        # Actually test with direct experiment_runner calls
        seed = 123
        count = 50
        # First profile
        settings.ADAPTIVE_POLICY_PROFILE = "revenue_first"
        run1 = experiment_runner.run_experiment(db, count=count, seed=seed)
        summary1 = experiment_runner.summarize_run(db, run1)
        baseline1 = summary1["arms"]["baseline"]
        # Clean DB for next run (keep run1 rows but they don't affect next baseline because scenario_rng is independent)
        # But to test invariance, we run second profile with same seed/count on a fresh DB state
        # We need to delete experiment cases for clean comparison, or just compare baseline1 vs baseline of second run
        settings.ADAPTIVE_POLICY_PROFILE = "balanced"
        run2 = experiment_runner.run_experiment(db, count=count, seed=seed)
        summary2 = experiment_runner.summarize_run(db, run2)
        baseline2 = summary2["arms"]["baseline"]
        assert baseline1["amount_at_risk"] == baseline2["amount_at_risk"]
        assert baseline1["action_distribution"] == baseline2["action_distribution"]
        assert baseline1["contacts"] == baseline2["contacts"]
        assert baseline1["escalations"] == baseline2["escalations"]
        assert baseline1["amount_recovered"] == baseline2["amount_recovered"]

        settings.ADAPTIVE_POLICY_PROFILE = "low_friction"
        run3 = experiment_runner.run_experiment(db, count=count, seed=seed)
        summary3 = experiment_runner.summarize_run(db, run3)
        baseline3 = summary3["arms"]["baseline"]
        assert baseline1["amount_at_risk"] == baseline3["amount_at_risk"]
        assert baseline1["contacts"] == baseline3["contacts"]
    finally:
        settings.ADAPTIVE_POLICY_PROFILE = old_profile
        settings.ADAPTIVE_FRICTION_WEIGHT = old_weight


def test_experiment_same_seed_reproducible(db):
    """Same seed/count repeated must give identical summary."""
    from app.services import experiment_runner
    seed = 999
    count = 30
    run1 = experiment_runner.run_experiment(db, count=count, seed=seed)
    summary1 = experiment_runner.summarize_run(db, run1)
    run2 = experiment_runner.run_experiment(db, count=count, seed=seed)
    summary2 = experiment_runner.summarize_run(db, run2)
    assert summary1["arms"]["baseline"]["amount_at_risk"] == summary2["arms"]["baseline"]["amount_at_risk"]
    assert summary1["arms"]["adaptive"]["amount_at_risk"] == summary2["arms"]["adaptive"]["amount_at_risk"]
    assert summary1["arms"]["baseline"]["action_distribution"] == summary2["arms"]["baseline"]["action_distribution"]
    assert summary1["arms"]["adaptive"]["action_distribution"] == summary2["arms"]["adaptive"]["action_distribution"]


def test_malformed_live_feature_triggers_adaptive_fallback(db, monkeypatch):
    """Malformed live feature row must become adaptive_fallback via dispatcher, not 500."""
    from app.ml import features as _features
    from app.services import ml_policy as _ml_policy

    def bad_build(db_, case_, action_, now_):
        # Missing required column and invalid type
        return {"failure_category": "UNKNOWN", "action_taken": action_, "amount": "not-a-number"}

    monkeypatch.setattr(_features, "build_live_features", bad_build)
    monkeypatch.setattr(_ml_policy, "build_live_features", bad_build)
    # scorer.validate_feature_row will raise ValueError/TypeError -> converted to ModelNotTrainedError
    old = settings.RECOVERY_POLICY
    settings.RECOVERY_POLICY = "adaptive"
    try:
        case = make_case(db, failure_category="TRANSIENT_INFRASTRUCTURE")
        # Ensure no prior fallback audit
        before = db.query(AuditEvent).filter(AuditEvent.event == "adaptive_fallback").count()
        decision = policy_dispatcher.decide_for_case(db, case, now=NOW)
        assert decision is not None
        assert decision.policy_mode == "adaptive_fallback"
        assert db.query(AuditEvent).filter(AuditEvent.event == "adaptive_fallback").count() == before + 1
        assert case.state not in ("DIAGNOSED", "DECISION_READY")
        # No generic exception escapes
    finally:
        settings.RECOVERY_POLICY = old


def test_model_manifest_exists():
    from pathlib import Path
    from app.ml.train import MANIFEST_PATH
    import json
    assert MANIFEST_PATH.exists(), "manifest.json should exist after train"
    data = json.loads(MANIFEST_PATH.read_text())
    assert data["model_version"] == "recovery-v1"
    assert data["feature_schema_version"] == "v1"
    assert "fingerprint" in data
    assert data["synthetic_data_notice"] is not None
