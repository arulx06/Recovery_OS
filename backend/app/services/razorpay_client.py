"""
Razorpay Payment Links client (Phase 4).

Real Razorpay test-mode credentials (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)
aren't something every environment running this code will have wired in
yet — a fresh clone, CI, or this repo before someone's pasted in their own
test keys. Rather than hard-failing the whole recovery pipeline whenever
credentials are absent, this client falls back to a clearly-labeled local
simulation: it fabricates a payment-link-shaped response with `simulated:
True` so the rest of the system (Action.result, the audit trail, the
dashboard) can show real behavior end to end. The moment real credentials
are set, it calls the actual API — no code change required, and nothing
about the simulation path can be mistaken for a live link because every
caller checks the `simulated` flag before treating a URL as real.

This is unlike webhook signature verification, which fails closed when no
secret is configured — that's a security boundary. This isn't; it's a
missing integration, so degrading gracefully is the right default.
"""
import time
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
        "note": "RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not configured — this is a local "
                "simulation, not a real Razorpay Payment Link. Set real test-mode "
                "credentials to switch to live calls.",
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
    configured) for the given amount. reference_id should be the
    RevenueCase id, so a paid webhook can be traced back even before we
    match on the returned payment_link id.

    Returns the Razorpay API response dict (or the simulated equivalent).
    Raises RazorpayAPIError on a live call that fails.
    """
    if not credentials_configured():
        return _simulated_payment_link(amount_rupees, currency, description, reference_id)

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
