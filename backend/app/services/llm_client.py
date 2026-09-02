"""
LLM client (Phase 6).

Same shape as razorpay_client.py: calls a real API when credentials are
configured (Anthropic's Messages API, since api.anthropic.com is the one
LLM endpoint actually reachable from this environment), and falls back to
a clearly-labeled local simulation otherwise — every response carries a
`simulated` flag so nothing downstream can mistake a heuristic stand-in
for a real model output.

Two jobs, matching ARCHITECTURE.md's layer table:
  - draft_contact_message: action + case context -> outbound message text
  - extract_ptp_intent: a customer's free-text reply -> structured intent
    ({promise_to_pay, dispute, unclear}, amount, date, confidence)

The LLM only ever produces text or a structured guess — it never decides
whether money moves. extract_ptp_intent's output is validated by
ptp_extractor.py before anything is recorded; a dispute classification
here still has to route through the same case-state machine as a real
Razorpay dispute webhook. Nothing about this client can move money by
itself.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.core.config import settings

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-3-5-haiku-latest"  # fast/cheap is plenty for drafting + extraction

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

DISPUTE_PHRASES = [
    "already paid", "already been paid", "double charge", "double charged",
    "didn't authorize", "did not authorize", "not mine", "wrong amount",
    "incorrect amount", "dispute", "mistake", "fraud", "unauthorized",
    "never made this", "don't recognize", "do not recognize",
]

AMOUNT_PATTERN = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
BARE_NUMBER_NEAR_PAY_PATTERN = re.compile(
    r"\b(?:pay|send|give|clear|settle)\w*\s+(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE,
)


class LLMAPIError(Exception):
    """Raised when a live call to the LLM API fails."""


def credentials_configured() -> bool:
    return bool(settings.LLM_API_KEY)


@dataclass
class PTPExtraction:
    intent: str  # "promise_to_pay" | "dispute" | "unclear"
    amount: float | None = None
    date: str | None = None  # ISO date string
    confidence: float = 0.0
    simulated: bool = True
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Message drafting
# ---------------------------------------------------------------------------

_TEMPLATES = {
    "CONTACT_CUSTOMER": (
        "Hi, we noticed your recent payment of ₹{amount} didn't go through "
        "({reason}). Could you let us know when you'd be able to complete it, "
        "or reply here if something looks wrong?"
    ),
    "COLLECT_PROMISE_TO_PAY": (
        "Hi, your payment of ₹{amount} is still pending. If you're able to "
        "pay soon, just let us know what day works for you and we'll hold "
        "off on any further reminders until then."
    ),
}


def _simulated_draft(action_type: str, context: dict) -> dict:
    template = _TEMPLATES.get(action_type, _TEMPLATES["CONTACT_CUSTOMER"])
    body = template.format(
        amount=f"{context.get('amount', 0):,.2f}",
        reason=(context.get("failure_category") or "a payment issue").replace("_", " ").lower(),
    )
    return {"body": body, "simulated": True, "note": "LLM_API_KEY not configured — templated message, not model-generated."}


def draft_contact_message(action_type: str, context: dict, timeout_seconds: float = 10.0) -> dict:
    """
    context: {"amount": float, "failure_category": str, "case_id": str, ...}
    Returns {"body": str, "simulated": bool, ...}
    """
    if not credentials_configured():
        return _simulated_draft(action_type, context)

    system_prompt = (
        "You draft short, plain, compliant payment-recovery messages to customers whose "
        "payment failed. Be warm but brief (2-3 sentences), never threatening, never "
        "mention penalties. Ask what day they can pay, or invite them to flag if the "
        "charge looks wrong. Output only the message body, no preamble."
    )
    user_prompt = (
        f"Failed payment context: amount=₹{context.get('amount')}, "
        f"failure_category={context.get('failure_category')}, action={action_type}. "
        "Draft the outbound message."
    )

    try:
        response = httpx.post(
            ANTHROPIC_API_URL,
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            headers={
                "x-api-key": settings.LLM_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        body = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        return {"body": body.strip(), "simulated": False}
    except httpx.HTTPError as exc:
        raise LLMAPIError(f"LLM message draft failed: {exc}") from exc


# ---------------------------------------------------------------------------
# PTP / dispute extraction
# ---------------------------------------------------------------------------

def _parse_relative_date(text: str, now: datetime) -> str | None:
    text = text.lower()
    if "tomorrow" in text:
        return (now + timedelta(days=1)).date().isoformat()
    if "today" in text or "tonight" in text:
        return now.date().isoformat()

    for i, day in enumerate(WEEKDAYS):
        if day in text:
            days_ahead = (i - now.weekday()) % 7
            days_ahead = days_ahead or 7  # "friday" mentioned on a Friday means *next* Friday
            return (now + timedelta(days=days_ahead)).date().isoformat()

    return None


def _parse_amount(text: str) -> float | None:
    m = AMOUNT_PATTERN.search(text)
    if not m:
        m = BARE_NUMBER_NEAR_PAY_PATTERN.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _simulated_extract(message: str, now: datetime) -> PTPExtraction:
    lowered = message.lower()

    if any(phrase in lowered for phrase in DISPUTE_PHRASES):
        return PTPExtraction(intent="dispute", confidence=0.9, simulated=True, raw={"matched_phrase": True})

    amount = _parse_amount(message)
    date = _parse_relative_date(message, now)

    if amount is not None and date is not None:
        return PTPExtraction(intent="promise_to_pay", amount=amount, date=date, confidence=0.85, simulated=True)

    if amount is not None or date is not None:
        # Partial signal — e.g. a date with no amount, or vice versa. Not
        # confident enough to record automatically.
        return PTPExtraction(
            intent="unclear", amount=amount, date=date, confidence=0.4, simulated=True,
            raw={"partial_match": True},
        )

    return PTPExtraction(intent="unclear", confidence=0.2, simulated=True)


def extract_ptp_intent(message: str, now: datetime | None = None, timeout_seconds: float = 10.0) -> PTPExtraction:
    now = now or datetime.utcnow()

    if not credentials_configured():
        return _simulated_extract(message, now)

    system_prompt = (
        "Extract intent from a customer's reply about an overdue payment. Respond with ONLY "
        "a JSON object, no other text: "
        '{"intent": "promise_to_pay" | "dispute" | "unclear", "amount": number or null, '
        '"date": "YYYY-MM-DD" or null, "confidence": number between 0 and 1}. '
        f"Today's date is {now.date().isoformat()}. \"dispute\" means the customer says the "
        "charge is wrong, already paid, or unauthorized. \"promise_to_pay\" requires both a "
        "clear amount and a clear date; otherwise use \"unclear\"."
    )

    try:
        response = httpx.post(
            ANTHROPIC_API_URL,
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 200,
                "system": system_prompt,
                "messages": [{"role": "user", "content": message}],
            },
            headers={
                "x-api-key": settings.LLM_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        parsed = json.loads(text)
        return PTPExtraction(
            intent=parsed.get("intent", "unclear"),
            amount=parsed.get("amount"),
            date=parsed.get("date"),
            confidence=float(parsed.get("confidence", 0.0)),
            simulated=False,
            raw=parsed,
        )
    except httpx.HTTPError as exc:
        raise LLMAPIError(f"LLM PTP extraction failed: {exc}") from exc
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        # The model didn't return valid structured output — treat as
        # unclear rather than crash the pipeline on a malformed response.
        return PTPExtraction(intent="unclear", confidence=0.0, simulated=False, raw={"parse_error": str(exc)})
