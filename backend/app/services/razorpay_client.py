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


class RazorpayAmbiguousError(RazorpayAPIError):
    """Provider outcome unknown — must reconcile by reference before retrying."""


def credentials_configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def _sanitize_link(link: dict) -> dict:
    """Return only fields safe to persist; drop provider internals.

    Keeps 'simulated' marker if the caller supplied it (simulation path) but
    never persists auth headers or secrets.
    """
    allowed = {
        "id", "reference_id", "short_url", "amount", "currency",
        "description", "notes", "status", "created_at", "expire_by",
        "amount_paid", "order_id", "upi_link", "simulated",
    }
    return {k: v for k, v in link.items() if k in allowed}


def _is_duplicate_reference_error(exc: httpx.HTTPStatusError) -> bool:
    try:
        body = exc.response.text or ""
        low = body.lower()
        # Razorpay docs: "payment link creation with reference ID already attempted"
        # HTTP 400 "An existing reference id has been passed."
        return "reference" in low and ("already" in low or "exists" in low or "duplicate" in low)
    except Exception:
        return False


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
        data = response.json()
        sanitized = _sanitize_link(data)
        # Preserve any safe provider id/short_url that sanitization may have kept;
        # do not invent fields.
        return sanitized
    except httpx.TimeoutException as exc:
        raise RazorpayAmbiguousError(f"Razorpay payment_links create timed out (outcome unknown): {exc}") from exc
    except httpx.NetworkError as exc:
        raise RazorpayAmbiguousError(f"Razorpay payment_links create network error (outcome unknown): {exc}") from exc
    except httpx.ConnectError as exc:
        raise RazorpayAmbiguousError(f"Razorpay payment_links create connection error (outcome unknown): {exc}") from exc
    except httpx.HTTPStatusError as exc:
        # Duplicate reference_id means the provider already accepted this Action's
        # request on a prior attempt — treat as ambiguous/reconcile, not definite failure.
        if _is_duplicate_reference_error(exc):
            raise RazorpayAmbiguousError(f"Razorpay duplicate reference_id (reconcile required): {exc}") from exc
        status = exc.response.status_code if exc.response is not None else None
        # 5xx / 429 are transient but still ambiguous w.r.t. whether the provider
        # committed the link before failing the response.
        if status is not None and (500 <= status < 600 or status == 429):
            raise RazorpayAmbiguousError(f"Razorpay payment_links transient failure (reconcile required): {exc}") from exc
        raise RazorpayAPIError(f"Razorpay payment_links create failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RazorpayAPIError(f"Razorpay payment_links create failed: {exc}") from exc


def build_provider_reference(action_id: str) -> str:
    """Canonical Action-scoped provider identity.

    Must be stable for one CREATE_PAYMENT_LINK Action across retries/reconciliation.
    Uses the durable Action.id directly (UUID, 36 chars) which fits Razorpay's
    40-char reference_id limit. One RevenueCase may have many Actions, but one
    Action maps to one intended provider Payment Link.
    """
    return action_id


def fetch_payment_link(payment_link_id: str, timeout_seconds: float = 10.0) -> dict:
    """GET /v1/payment_links/{id} — live only; simulation has no provider state."""
    if not settings.RAZORPAY_API_ENABLED:
        raise RazorpayAPIError("fetch_payment_link unavailable in simulation mode")
    if not credentials_configured():
        raise RazorpayAPIError("Razorpay API access is enabled but credentials incomplete")
    if not settings.RAZORPAY_KEY_ID.startswith("rzp_test_"):
        raise RazorpayAPIError("RecoveryOS only permits Razorpay Test Mode API keys")
    if not payment_link_id:
        raise RazorpayAPIError("payment_link_id is required")
    try:
        response = httpx.get(
            f"{BASE_URL}/payment_links/{payment_link_id}",
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return _sanitize_link(response.json())
    except httpx.TimeoutException as exc:
        raise RazorpayAmbiguousError(f"fetch payment_link timed out: {exc}") from exc
    except httpx.NetworkError as exc:
        raise RazorpayAmbiguousError(f"fetch payment_link network error: {exc}") from exc
    except httpx.ConnectError as exc:
        raise RazorpayAmbiguousError(f"fetch payment_link connection error: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and (500 <= status < 600 or status == 429):
            raise RazorpayAmbiguousError(f"fetch payment_link transient failure: {exc}") from exc
        raise RazorpayAPIError(f"fetch payment_link failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RazorpayAPIError(f"fetch payment_link failed: {exc}") from exc


def list_payment_links_by_reference(reference_id: str, timeout_seconds: float = 10.0) -> list[dict]:
    """GET /v1/payment_links?reference_id=X — the provider-supported lookup.

    Razorpay enforces uniqueness on reference_id (400 on duplicate create) and
    supports filtering by reference_id on the list endpoint. This is the
    strongest safe primitive for provider-side reconciliation.

    Returns a sanitized list (may be empty). Caller must validate amount/currency/notes.
    """
    if not settings.RAZORPAY_API_ENABLED:
        return []
    if not credentials_configured():
        raise RazorpayAPIError("credentials incomplete")
    if not settings.RAZORPAY_KEY_ID.startswith("rzp_test_"):
        raise RazorpayAPIError("RecoveryOS only permits Razorpay Test Mode API keys")
    if not reference_id:
        return []
    try:
        response = httpx.get(
            f"{BASE_URL}/payment_links",
            params={"reference_id": reference_id},
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "payment_links" in data:
            raw = data["payment_links"]
        elif isinstance(data, list):
            raw = data
        else:
            raw = []
        return [_sanitize_link(item) if isinstance(item, dict) else {} for item in raw]
    except httpx.TimeoutException as exc:
        raise RazorpayAmbiguousError(f"list payment_links timed out: {exc}") from exc
    except httpx.NetworkError as exc:
        raise RazorpayAmbiguousError(f"list payment_links network error: {exc}") from exc
    except httpx.ConnectError as exc:
        raise RazorpayAmbiguousError(f"list payment_links connection error: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and (500 <= status < 600 or status == 429):
            raise RazorpayAmbiguousError(f"list payment_links transient failure: {exc}") from exc
        raise RazorpayAPIError(f"list payment_links failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RazorpayAPIError(f"list payment_links failed: {exc}") from exc


def find_payment_link_for_action(
    action_id: str,
    expected_amount_paise: int,
    expected_currency: str,
    expected_case_id: str,
    timeout_seconds: float = 10.0,
) -> dict | None:
    """Reconcile: find the provider Payment Link for one Action, validated.

    Returns the sanitized provider link if a single matching link exists and
    passes all validation, otherwise None. Raises on mismatch (caller should
    route to HUMAN_REVIEW) or on ambiguous lookup failure (caller should
    retry reconciliation later).
    """
    links = list_payment_links_by_reference(action_id, timeout_seconds=timeout_seconds)
    if not links:
        return None
    if len(links) > 1:
        raise RazorpayAPIError(f"multiple provider links found for reference {action_id}")

    link = links[0]
    # Validate stable business identity — do not adopt an unrelated link.
    if link.get("reference_id") != action_id:
        raise RazorpayAPIError(
            f"provider link reference_id mismatch: expected {action_id}, got {link.get('reference_id')}"
        )
    provider_amount = link.get("amount")
    if provider_amount is not None and int(provider_amount) != int(expected_amount_paise):
        raise RazorpayAPIError(
            f"provider link amount mismatch for {action_id}: expected {expected_amount_paise}, got {provider_amount}"
        )
    provider_currency = link.get("currency")
    if provider_currency and provider_currency != expected_currency:
        raise RazorpayAPIError(
            f"provider link currency mismatch for {action_id}: expected {expected_currency}, got {provider_currency}"
        )
    notes = link.get("notes") or {}
    # If provider notes carry our deterministic ids, they must match. If notes
    # are empty/missing (older links), allow match on reference+amount+currency.
    if isinstance(notes, dict):
        note_case = notes.get("recoveryos_case_id")
        note_action = notes.get("recoveryos_action_id")
        if note_case is not None and note_case != expected_case_id:
            raise RazorpayAPIError(
                f"provider link notes case mismatch for {action_id}: expected {expected_case_id}, got {note_case}"
            )
        if note_action is not None and note_action != action_id:
            raise RazorpayAPIError(
                f"provider link notes action mismatch for {action_id}: expected {action_id}, got {note_action}"
            )

    # Status validation — only adopt links that are still usable. Expired/cancelled
    # links must not be silently adopted as success; route to manual review.
    status = (link.get("status") or "").lower()
    if status in ("expired", "cancelled"):
        raise RazorpayAPIError(
            f"provider link {link.get('id')} for {action_id} is {status} — manual review required"
        )

    return link
