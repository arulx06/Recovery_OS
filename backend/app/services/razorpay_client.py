"""
Razorpay Payment Links client (Phase 4).

This client uses a clearly-labeled local simulation unless Razorpay API
access is explicitly enabled. Credentials alone never activate network
traffic. Live access is limited to Razorpay Test Mode keys.

This is unlike webhook signature verification, which fails closed when no
secret is configured — that's a security boundary. This isn't; it's a
missing integration, so degrading gracefully is the right default.
"""
import uuid
from decimal import Decimal
from typing import Optional

import httpx

from app.core.config import settings

BASE_URL = "https://api.razorpay.com/v1"


class RazorpayAPIError(Exception):
    """Raised when a live call to the Razorpay API fails."""


def credentials_configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def _simulated_payment_link(amount_rupees: Decimal, currency: str, description: str, reference_id: str) -> dict:
    fake_id = f"plink_sim_{uuid.uuid4().hex[:14]}"
    return {
        "id": fake_id,
        "short_url": f"https://rzp.io/simulated/{fake_id}",
        "amount": int(amount_rupees * 100),
        "currency": currency,
        "description": description,
        "reference_id": reference_id,
        "status": "created",
        "simulated": True,
        "note": "Razorpay API access is disabled - this is a local simulation, not a real "
                "Payment Link. Set Test Mode credentials and RAZORPAY_API_ENABLED=true "
                "to enable external calls.",
    }


def create_payment_link(
    amount_rupees: Decimal,
    currency: str,
    description: str,
    reference_id: str,
    notes: Optional[dict] = None,
    timeout_seconds: float = 10.0,
) -> dict:
    """
    Creates a Razorpay Payment Link (test mode, once real credentials are
    configured) for the given amount. The runtime uses the durable Action id
    as reference_id and includes both action/case ids in notes so retries and
    provider-side reconciliation have a deterministic key.

    Returns the Razorpay API response dict (or the simulated equivalent).
    Raises RazorpayAPIError on a live call that fails.
    """
    if not settings.RAZORPAY_API_ENABLED:
        return _simulated_payment_link(amount_rupees, currency, description, reference_id)

    if not credentials_configured():
        raise RazorpayAPIError(
            "Razorpay API access is enabled but RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are incomplete"
        )
    if not settings.RAZORPAY_KEY_ID.startswith("rzp_test_"):
        raise RazorpayAPIError("RecoveryOS only permits Razorpay Test Mode API keys")

    payload = {
        "amount": int(amount_rupees * 100),  # paise
        "currency": currency,
        "description": description,
        "reference_id": reference_id,
        "notes": notes or {},
        "callback_method": "get",
    }

    try:
        response = httpx.post(
            f"{BASE_URL}/payment_links",
            json=payload,
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise RazorpayAPIError(f"Razorpay payment_links create failed: {exc}") from exc
