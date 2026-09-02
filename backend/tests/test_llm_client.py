from datetime import datetime

import httpx
import pytest

from app.core.config import settings
from app.services import llm_client


@pytest.fixture(autouse=True)
def _clear_credentials():
    original = settings.LLM_API_KEY
    settings.LLM_API_KEY = ""
    yield
    settings.LLM_API_KEY = original


NOW = datetime(2026, 8, 23, 10, 0, 0)  # a Sunday


# ---- credentials / simulation fallback ----

def test_credentials_configured_false_when_empty():
    assert llm_client.credentials_configured() is False


def test_credentials_configured_true_when_set():
    settings.LLM_API_KEY = "sk-ant-test"
    assert llm_client.credentials_configured() is True


# ---- draft_contact_message ----

def test_simulated_draft_is_labeled():
    result = llm_client.draft_contact_message("CONTACT_CUSTOMER", {"amount": 1000, "failure_category": "INVALID_INSTRUMENT"})
    assert result["simulated"] is True
    assert "1,000.00" in result["body"]


def test_simulated_draft_differs_by_action_type():
    contact = llm_client.draft_contact_message("CONTACT_CUSTOMER", {"amount": 500, "failure_category": "X"})
    ptp = llm_client.draft_contact_message("COLLECT_PROMISE_TO_PAY", {"amount": 500, "failure_category": "X"})
    assert contact["body"] != ptp["body"]


def test_live_draft_used_when_credentials_present(monkeypatch):
    settings.LLM_API_KEY = "sk-ant-test"

    def fake_post(url, json, headers, timeout):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"content": [{"type": "text", "text": "Hi, your payment is pending — when can you pay?"}]}

        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm_client.draft_contact_message("CONTACT_CUSTOMER", {"amount": 1000, "failure_category": "X"})
    assert result["simulated"] is False
    assert "when can you pay" in result["body"]


def test_live_draft_failure_raises(monkeypatch):
    settings.LLM_API_KEY = "sk-ant-test"

    def fake_post(*args, **kwargs):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(llm_client.LLMAPIError):
        llm_client.draft_contact_message("CONTACT_CUSTOMER", {"amount": 1000, "failure_category": "X"})


# ---- extract_ptp_intent (simulated heuristics) ----

@pytest.mark.parametrize("message,expected_intent,expected_amount,expected_date", [
    ("I'll pay 8000 Friday", "promise_to_pay", 8000.0, "2026-08-28"),
    ("I'll pay ₹8,000 on Friday", "promise_to_pay", 8000.0, "2026-08-28"),
    ("I can send Rs. 500 tomorrow", "promise_to_pay", 500.0, "2026-08-24"),
    ("can pay 12000 today", "promise_to_pay", 12000.0, "2026-08-23"),
    ("I'll clear INR 999 on monday", "promise_to_pay", 999.0, "2026-08-24"),
])
def test_simulated_extraction_promise_to_pay(message, expected_intent, expected_amount, expected_date):
    result = llm_client.extract_ptp_intent(message, now=NOW)
    assert result.intent == expected_intent
    assert result.amount == expected_amount
    assert result.date == expected_date
    assert result.confidence >= 0.6
    assert result.simulated is True


@pytest.mark.parametrize("message", [
    "this is wrong, I already paid this",
    "I never made this purchase, please investigate",
    "that's a double charge, please refund",
    "I did not authorize this payment",
    "this looks like fraud",
])
def test_simulated_extraction_dispute(message):
    result = llm_client.extract_ptp_intent(message, now=NOW)
    assert result.intent == "dispute"
    assert result.confidence >= 0.8


@pytest.mark.parametrize("message", [
    "not sure what this is for",
    "I'll pay next week sometime",
    "ok",
    "",
    "why was I charged",
])
def test_simulated_extraction_unclear(message):
    result = llm_client.extract_ptp_intent(message, now=NOW)
    assert result.intent == "unclear"


def test_simulated_extraction_partial_amount_only_is_unclear():
    result = llm_client.extract_ptp_intent("I can pay 500", now=NOW)
    assert result.intent == "unclear"
    assert result.amount == 500.0
    assert result.date is None


def test_bare_weekday_on_that_weekday_means_next_week():
    # NOW is a Sunday; mentioning "sunday" should mean next Sunday, not today.
    result = llm_client.extract_ptp_intent("I'll pay 100 on sunday", now=NOW)
    assert result.date == "2026-08-30"


def test_live_extraction_used_when_credentials_present(monkeypatch):
    settings.LLM_API_KEY = "sk-ant-test"

    def fake_post(url, json, headers, timeout):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"content": [{"type": "text", "text": '{"intent": "promise_to_pay", "amount": 8000, "date": "2026-08-28", "confidence": 0.92}'}]}

        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm_client.extract_ptp_intent("I'll pay Friday", now=NOW)
    assert result.simulated is False
    assert result.intent == "promise_to_pay"
    assert result.amount == 8000
    assert result.confidence == 0.92


def test_live_extraction_malformed_json_falls_back_to_unclear(monkeypatch):
    settings.LLM_API_KEY = "sk-ant-test"

    def fake_post(url, json, headers, timeout):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"content": [{"type": "text", "text": "not valid json"}]}

        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm_client.extract_ptp_intent("whatever", now=NOW)
    assert result.intent == "unclear"
    assert result.simulated is False


def test_live_extraction_failure_raises(monkeypatch):
    settings.LLM_API_KEY = "sk-ant-test"

    def fake_post(*args, **kwargs):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(llm_client.LLMAPIError):
        llm_client.extract_ptp_intent("whatever", now=NOW)
