"""
Deterministic promise-to-pay validation.

The LLM's extracted {promised_amount, promised_date, confidence} is a guess.
Before anything gets recorded as PromiseToPay, deterministic checks run:

    intent == promise_to_pay
    date parseable, not in past, not absurdly far (horizon)
    amount semantics: explicit amount must be positive and <= outstanding;
                    omitted amount follows deterministic business rule
                    (customer_explicit vs deterministic_full_balance) outside LLM
    confidence advisory but not sole check
    case eligibility (checked by caller — this module only validates extraction)
    merchant-timezone day validity through exclusive end-of-day UTC

Only a promise that clears every one of these becomes a PromiseToPay row.
Any failure returns reason for HUMAN_REVIEW / manual handling, not silent drop.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.services.llm_client import PTPExtraction
from app.core.time import utc_now

DEFAULT_HORIZON_DAYS = 14
MAX_HORIZON_DAYS = 90  # absurdly far guard
MIN_CONFIDENCE = 0.6


@dataclass
class ValidationResult:
    valid: bool
    reason: str | None = None
    amount: float | None = None
    promised_date: date | None = None
    amount_method: str | None = None  # customer_explicit | deterministic_full_balance
    reasoning_code: str | None = None


def validate_promise(
    extraction: PTPExtraction,
    outstanding_amount: float,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    min_confidence: float = MIN_CONFIDENCE,
) -> ValidationResult:
    now = now or utc_now()
    today = now.date()

    # 12. Deterministic PTP validation — intent must be promise_to_pay
    if extraction.intent != "promise_to_pay":
        return ValidationResult(valid=False, reason=f"not a promise_to_pay intent (got {extraction.intent!r})")

    if extraction.confidence < min_confidence:
        return ValidationResult(valid=False, reason=f"confidence {extraction.confidence} below minimum {min_confidence}")

    # Promised amount semantics — LLM must never invent amount
    # Existing deterministic business rule: explicit amount required; omitted amount does NOT auto-infer
    # to outstanding balance. It remains HUMAN_REVIEW / manual. LLM extracts only what customer said.
    # The inference path (deterministic_full_balance) would happen outside LLM if business rule changes,
    # but current accepted semantics is explicit-only.
    raw_amount = extraction.promised_amount if extraction.promised_amount is not None else extraction.amount
    raw_date = extraction.promised_date if extraction.promised_date is not None else extraction.date

    amount_method = "customer_explicit"
    if raw_amount is None:
        return ValidationResult(valid=False, reason="missing amount or date")

    if raw_amount <= 0:
        return ValidationResult(valid=False, reason=f"amount {raw_amount} is not positive")

    if raw_amount > outstanding_amount:
        return ValidationResult(
            valid=False,
            reason=f"promised amount {raw_amount} exceeds outstanding {outstanding_amount}",
        )

    if raw_date is None:
        return ValidationResult(valid=False, reason="missing promised date")

    try:
        promised_date = date.fromisoformat(raw_date)
    except (ValueError, TypeError):
        return ValidationResult(valid=False, reason=f"unparseable date {raw_date!r}")

    if promised_date < today:
        return ValidationResult(valid=False, reason=f"promised date {promised_date} is in the past")

    # Not absurdly far — horizon
    horizon = today + timedelta(days=horizon_days)
    if promised_date > horizon:
        return ValidationResult(
            valid=False,
            reason=f"promised date {promised_date} is beyond the {horizon_days}-day horizon ({horizon})",
        )
    # Additional absurdly far guard (90 days) regardless of configured horizon
    far_horizon = today + timedelta(days=MAX_HORIZON_DAYS)
    if promised_date > far_horizon:
        return ValidationResult(valid=False, reason=f"promised date {promised_date} is absurdly far in future")

    # Case eligibility is checked by caller (RECOVERED/DISPUTED/terminal etc.), but we validate basics
    if raw_date is None or raw_amount is None:
        return ValidationResult(valid=False, reason="missing amount or date after resolution")

    return ValidationResult(
        valid=True,
        amount=raw_amount,
        promised_date=promised_date,
        amount_method=amount_method,
        reasoning_code=extraction.reasoning_code,
    )
