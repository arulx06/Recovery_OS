from datetime import datetime

from app.services.llm_client import PTPExtraction
from app.services.ptp_extractor import validate_promise

NOW = datetime(2026, 8, 23, 10, 0, 0)  # a Sunday


def valid_extraction(**overrides):
    defaults = dict(intent="promise_to_pay", amount=5000.0, date="2026-08-28", confidence=0.85)
    defaults.update(overrides)
    return PTPExtraction(**defaults)


def test_valid_promise_passes():
    result = validate_promise(valid_extraction(), outstanding_amount=8000.0, now=NOW)
    assert result.valid is True
    assert result.amount == 5000.0
    assert result.promised_date.isoformat() == "2026-08-28"


def test_non_promise_intent_rejected():
    result = validate_promise(PTPExtraction(intent="dispute", confidence=0.9), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "not a promise_to_pay" in result.reason


def test_low_confidence_rejected():
    result = validate_promise(valid_extraction(confidence=0.4), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "confidence" in result.reason


def test_missing_amount_rejected():
    result = validate_promise(valid_extraction(amount=None), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "missing" in result.reason


def test_missing_date_rejected():
    result = validate_promise(valid_extraction(date=None), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "missing" in result.reason


def test_zero_or_negative_amount_rejected():
    result = validate_promise(valid_extraction(amount=0), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "not positive" in result.reason


def test_amount_exceeding_outstanding_rejected():
    result = validate_promise(valid_extraction(amount=9000.0), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "exceeds outstanding" in result.reason


def test_amount_exactly_equal_to_outstanding_passes():
    result = validate_promise(valid_extraction(amount=8000.0), outstanding_amount=8000.0, now=NOW)
    assert result.valid is True


def test_unparseable_date_rejected():
    result = validate_promise(valid_extraction(date="not-a-date"), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "unparseable" in result.reason


def test_past_date_rejected():
    result = validate_promise(valid_extraction(date="2026-08-01"), outstanding_amount=8000.0, now=NOW)
    assert result.valid is False
    assert "past" in result.reason


def test_today_is_allowed():
    result = validate_promise(valid_extraction(date="2026-08-23"), outstanding_amount=8000.0, now=NOW)
    assert result.valid is True


def test_date_beyond_horizon_rejected():
    result = validate_promise(valid_extraction(date="2026-09-30"), outstanding_amount=8000.0, now=NOW, horizon_days=14)
    assert result.valid is False
    assert "horizon" in result.reason


def test_date_at_exact_horizon_boundary_passes():
    # NOW is 2026-08-23, horizon_days=14 -> boundary is 2026-09-06
    result = validate_promise(valid_extraction(date="2026-09-06"), outstanding_amount=8000.0, now=NOW, horizon_days=14)
    assert result.valid is True


def test_custom_min_confidence_respected():
    result = validate_promise(valid_extraction(confidence=0.7), outstanding_amount=8000.0, now=NOW, min_confidence=0.8)
    assert result.valid is False
