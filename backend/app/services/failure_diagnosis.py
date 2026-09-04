"""
Deterministic failure intelligence engine.

Deterministic on purpose — per ARCHITECTURE.md, don't spend an LLM call on
structured fields. Razorpay already gives us `error_source`, `error_step`,
and `error_reason` on every failed payment; this module's only job is to
turn that triple (plus a little context) into one of a small, fixed set of
categories that the guardrail and policy engines can act on.

Rules are evaluated in order — first match wins — because some fields are
more diagnostic than others (a specific error_reason beats a generic
error_source). Anything that matches nothing falls through to UNKNOWN,
which the policy engine treats conservatively (human review)
rather than guessing.
"""
import re

# Canonical categories. Keep this list in sync with ARCHITECTURE.md's
# failure taxonomy table.
TRANSIENT_INFRASTRUCTURE = "TRANSIENT_INFRASTRUCTURE"
INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
CUSTOMER_AUTHENTICATION = "CUSTOMER_AUTHENTICATION"
INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
MANDATE_ISSUE = "MANDATE_ISSUE"
SUBSCRIPTION_PENDING_NATIVE_RETRY = "SUBSCRIPTION_PENDING_NATIVE_RETRY"
PERMANENT_HARD_FAILURE = "PERMANENT_HARD_FAILURE"
UNKNOWN = "UNKNOWN"

ALL_CATEGORIES = {
    TRANSIENT_INFRASTRUCTURE, INSUFFICIENT_BALANCE, CUSTOMER_AUTHENTICATION,
    INVALID_INSTRUMENT, MANDATE_ISSUE, SUBSCRIPTION_PENDING_NATIVE_RETRY,
    PERMANENT_HARD_FAILURE, UNKNOWN,
}

# Each rule: (category, predicate(error_source, error_step, error_reason, entity) -> bool)
# error_source/error_step/error_reason are lowercased strings (possibly "").


def _contains_any(text: str, *needles: str) -> bool:
    return any(n in text for n in needles)


_RULES = [
    (
        INSUFFICIENT_BALANCE,
        lambda src, step, reason, e: _contains_any(reason, "insufficient", "low_balance", "no_funds"),
    ),
    (
        MANDATE_ISSUE,
        lambda src, step, reason, e: _contains_any(reason, "mandate") or _contains_any(step, "mandate"),
    ),
    (
        CUSTOMER_AUTHENTICATION,
        lambda src, step, reason, e: _contains_any(
            reason, "otp", "authentication", "3ds", "incorrect_pin", "pin_incorrect", "auth_failed"
        ) or step == "payment_authentication",
    ),
    (
        INVALID_INSTRUMENT,
        lambda src, step, reason, e: _contains_any(
            reason, "expired", "invalid_card", "card_blocked", "invalid_vpa", "invalid_account", "restricted_card"
        ),
    ),
    (
        PERMANENT_HARD_FAILURE,
        lambda src, step, reason, e: _contains_any(reason, "risk", "fraud", "blocked_by_risk", "blacklist"),
    ),
    (
        # Specific transient-infra signals take priority over the
        # subscription-native-retry guess below, even for subscription
        # payments — a documented timeout is more informative than an
        # absence of information.
        TRANSIENT_INFRASTRUCTURE,
        lambda src, step, reason, e: _contains_any(
            reason, "timeout", "gateway_error", "server_error", "bank_server_error", "network_error"
        ),
    ),
    (
        # Subscription-linked failures with no specific reason and a
        # source outside the customer's control — best left to Razorpay's
        # own retry schedule rather than us doubly retrying. Checked
        # before the generic transient-infra catch-all so it isn't stolen
        # by that broader rule.
        SUBSCRIPTION_PENDING_NATIVE_RETRY,
        lambda src, step, reason, e: bool(e.get("subscription_id")) and reason == "" and src in ("bank", "network", "gateway", ""),
    ),
    (
        # Generic catch-all: no specific reason given, but the source
        # points away from the customer — treat as transient rather than
        # unknown.
        TRANSIENT_INFRASTRUCTURE,
        lambda src, step, reason, e: reason == "" and src in ("bank", "gateway", "network"),
    ),
]


def classify_failure(entity: dict) -> str:
    """
    entity: the Razorpay payment entity dict (error_source, error_step,
    error_reason, and whatever else was on the payload).
    """
    error_source = (entity.get("error_source") or "").strip().lower()
    error_step = (entity.get("error_step") or "").strip().lower()
    error_reason = (entity.get("error_reason") or "").strip().lower()
    # normalize separators so "Invalid Card" / "invalid-card" / "invalid_card" all match
    error_reason = re.sub(r"[\s-]+", "_", error_reason)

    for category, predicate in _RULES:
        if predicate(error_source, error_step, error_reason, entity):
            return category

    return UNKNOWN
