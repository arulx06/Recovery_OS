"""
Promise-to-Pay validation (Phase 6).

Per ARCHITECTURE.md: the LLM's extracted {amount, date, confidence} is a
guess, not an instruction. Before anything gets recorded as a
PromiseToPay, the backend checks:

    sum promised <= invoice outstanding
    date >= today
    date <= configured horizon
    customer/case still active (checked by the caller — this module only
    validates the extraction itself)
    confidence above a minimum bar

Only a promise that clears every one of these becomes a PromiseToPay row.
Anything that fails validation is NOT silently dropped — it's returned
with a reason, so the caller can fall back to human review instead of
either trusting a bad extraction or pretending nothing happened.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.services.llm_client import PTPExtraction
from app.core.time import utc_now

DEFAULT_HORIZON_DAYS = 14
MIN_CONFIDENCE = 0.6


@dataclass
class ValidationResult:
    valid: bool
    reason: str | None = None
    amount: float | None = None
    promised_date: date | None = None


def validate_promise(
    extraction: PTPExtraction,
    outstanding_amount: float,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    min_confidence: float = MIN_CONFIDENCE,
) -> ValidationResult:
    now = now or utc_now()
    today = now.date()

    if extraction.intent != "promise_to_pay":
        return ValidationResult(valid=False, reason=f"not a promise_to_pay intent (got {extraction.intent!r})")

    if extraction.confidence < min_confidence:
        return ValidationResult(valid=False, reason=f"confidence {extraction.confidence} below minimum {min_confidence}")

    if extraction.amount is None or extraction.date is None:
        return ValidationResult(valid=False, reason="missing amount or date")

    if extraction.amount <= 0:
        return ValidationResult(valid=False, reason=f"amount {extraction.amount} is not positive")

    if extraction.amount > outstanding_amount:
        return ValidationResult(
            valid=False,
            reason=f"promised amount {extraction.amount} exceeds outstanding {outstanding_amount}",
        )

    try:
        promised_date = date.fromisoformat(extraction.date)
    except (ValueError, TypeError):
        return ValidationResult(valid=False, reason=f"unparseable date {extraction.date!r}")

    if promised_date < today:
        return ValidationResult(valid=False, reason=f"promised date {promised_date} is in the past")

    horizon = today + timedelta(days=horizon_days)
    if promised_date > horizon:
        return ValidationResult(
            valid=False,
            reason=f"promised date {promised_date} is beyond the {horizon_days}-day horizon ({horizon})",
        )

    return ValidationResult(valid=True, amount=extraction.amount, promised_date=promised_date)
