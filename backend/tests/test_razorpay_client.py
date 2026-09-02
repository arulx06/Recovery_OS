from decimal import Decimal

import httpx
import pytest

from app.core.config import settings
from app.services import razorpay_client


@pytest.fixture(autouse=True)
def _clear_credentials():
    """Every test starts with no Razorpay credentials configured, unless
    it sets them itself — keeps the simulation-vs-live behavior explicit
    per test rather than depending on .env contents."""
    original_id, original_secret = settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET
    settings.RAZORPAY_KEY_ID = ""
    settings.RAZORPAY_KEY_SECRET = ""
    yield
    settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = original_id, original_secret


def test_credentials_configured_false_when_empty():
    assert razorpay_client.credentials_configured() is False


def test_credentials_configured_true_when_both_set():
    settings.RAZORPAY_KEY_ID = "rzp_test_x"
    settings.RAZORPAY_KEY_SECRET = "secret_x"
    assert razorpay_client.credentials_configured() is True


def test_falls_back_to_simulation_without_credentials():
    result = razorpay_client.create_payment_link(
        amount_rupees=Decimal("499.50"), currency="INR",
        description="test", reference_id="case_123",
    )
    assert result["simulated"] is True
    assert result["id"].startswith("plink_sim_")
    assert result["short_url"].startswith("https://rzp.io/simulated/")
    assert result["amount"] == 49950  # paise
    assert result["reference_id"] == "case_123"


def test_simulated_link_id_is_unique_per_call():
    a = razorpay_client.create_payment_link(Decimal("100"), "INR", "d", "ref1")
    b = razorpay_client.create_payment_link(Decimal("100"), "INR", "d", "ref2")
    assert a["id"] != b["id"]


def test_live_call_used_when_credentials_present(monkeypatch):
    settings.RAZORPAY_KEY_ID = "rzp_test_x"
    settings.RAZORPAY_KEY_SECRET = "secret_x"

    captured = {}

    def fake_post(url, json, auth, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = auth

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "plink_live_abc123", "short_url": "https://rzp.io/l/abc123", **json}

        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = razorpay_client.create_payment_link(
        amount_rupees=Decimal("1000"), currency="INR",
        description="live test", reference_id="case_live_1",
    )
    assert result["id"] == "plink_live_abc123"
    assert "simulated" not in result
    assert captured["url"].endswith("/payment_links")
    assert captured["json"]["amount"] == 100000
    assert captured["json"]["reference_id"] == "case_live_1"
    assert captured["auth"] == ("rzp_test_x", "secret_x")


def test_live_call_failure_raises_razorpay_api_error(monkeypatch):
    settings.RAZORPAY_KEY_ID = "rzp_test_x"
    settings.RAZORPAY_KEY_SECRET = "secret_x"

    def fake_post(*args, **kwargs):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(razorpay_client.RazorpayAPIError):
        razorpay_client.create_payment_link(Decimal("100"), "INR", "d", "ref")
