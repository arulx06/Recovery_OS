import pytest

from app.services.failure_diagnosis import (
    classify_failure,
    INSUFFICIENT_BALANCE,
    MANDATE_ISSUE,
    CUSTOMER_AUTHENTICATION,
    INVALID_INSTRUMENT,
    PERMANENT_HARD_FAILURE,
    TRANSIENT_INFRASTRUCTURE,
    SUBSCRIPTION_PENDING_NATIVE_RETRY,
    UNKNOWN,
)

SCENARIOS = [
    # (id, entity, expected_category)
    ("insufficient_funds_reason", {"error_reason": "insufficient_funds"}, INSUFFICIENT_BALANCE),
    ("low_balance_reason", {"error_reason": "low_balance"}, INSUFFICIENT_BALANCE),
    ("insufficient_funds_uppercase", {"error_reason": "INSUFFICIENT_FUNDS"}, INSUFFICIENT_BALANCE),

    ("mandate_cancelled_reason", {"error_reason": "mandate_cancelled"}, MANDATE_ISSUE),
    ("mandate_step_no_reason", {"error_step": "mandate_execution", "error_reason": ""}, MANDATE_ISSUE),
    ("mandate_revoked", {"error_reason": "mandate_revoked_by_customer"}, MANDATE_ISSUE),

    ("otp_incorrect", {"error_reason": "otp_incorrect"}, CUSTOMER_AUTHENTICATION),
    ("three_ds_failed", {"error_reason": "3ds_authentication_failed"}, CUSTOMER_AUTHENTICATION),
    ("auth_step_generic", {"error_step": "payment_authentication", "error_reason": ""}, CUSTOMER_AUTHENTICATION),
    ("pin_incorrect", {"error_reason": "pin_incorrect"}, CUSTOMER_AUTHENTICATION),

    ("card_expired", {"error_reason": "card_expired"}, INVALID_INSTRUMENT),
    ("invalid_card_underscore", {"error_reason": "invalid_card"}, INVALID_INSTRUMENT),
    ("invalid_card_hyphen", {"error_reason": "invalid-card"}, INVALID_INSTRUMENT),
    ("invalid_vpa", {"error_reason": "invalid_vpa"}, INVALID_INSTRUMENT),
    ("card_blocked", {"error_reason": "card_blocked_by_issuer"}, INVALID_INSTRUMENT),

    ("risk_check_failed", {"error_reason": "risk_check_failed"}, PERMANENT_HARD_FAILURE),
    ("blocked_by_risk", {"error_reason": "blocked_by_risk_engine"}, PERMANENT_HARD_FAILURE),
    ("fraud_suspected", {"error_reason": "fraud_suspected"}, PERMANENT_HARD_FAILURE),

    ("gateway_timeout", {"error_reason": "gateway_timeout"}, TRANSIENT_INFRASTRUCTURE),
    ("bank_server_error", {"error_reason": "bank_server_error"}, TRANSIENT_INFRASTRUCTURE),
    ("network_error_generic", {"error_reason": "network_error"}, TRANSIENT_INFRASTRUCTURE),
    ("empty_reason_bank_source", {"error_source": "bank", "error_reason": ""}, TRANSIENT_INFRASTRUCTURE),

    (
        "subscription_no_reason_bank_source",
        {"error_source": "bank", "error_reason": "", "subscription_id": "sub_123"},
        SUBSCRIPTION_PENDING_NATIVE_RETRY,
    ),
    (
        "subscription_no_reason_network_source",
        {"error_source": "network", "error_reason": "", "subscription_id": "sub_456"},
        SUBSCRIPTION_PENDING_NATIVE_RETRY,
    ),

    # Priority checks: a specific reason must win over the generic
    # subscription heuristic even when a subscription_id is present.
    (
        "subscription_but_specific_invalid_instrument_wins",
        {"error_reason": "card_expired", "subscription_id": "sub_789"},
        INVALID_INSTRUMENT,
    ),
    (
        "subscription_but_specific_transient_wins",
        {"error_reason": "gateway_timeout", "subscription_id": "sub_789"},
        TRANSIENT_INFRASTRUCTURE,
    ),

    ("unknown_customer_source_no_reason", {"error_source": "customer", "error_reason": ""}, UNKNOWN),
    ("unknown_completely_empty", {}, UNKNOWN),
    ("unknown_unrecognized_reason", {"error_reason": "something_new_razorpay_added"}, UNKNOWN),
]


@pytest.mark.parametrize("entity,expected", [(s[1], s[2]) for s in SCENARIOS], ids=[s[0] for s in SCENARIOS])
def test_classify_failure(entity, expected):
    assert classify_failure(entity) == expected


def test_all_scenarios_cover_every_public_category():
    from app.services import failure_diagnosis as fd
    covered = {expected for _, _, expected in SCENARIOS}
    assert covered == fd.ALL_CATEGORIES, f"missing coverage for: {fd.ALL_CATEGORIES - covered}"
