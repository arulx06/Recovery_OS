"""
Razorpay signs every webhook body with HMAC-SHA256 using the webhook secret
configured in the merchant dashboard. Verification must happen on the raw
request bytes — not on a re-serialized JSON dict, since key ordering /
whitespace differences would break the signature.
"""
import hashlib
import hmac


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not secret:
        # Dev fallback: if no webhook secret is configured yet (e.g. before
        # Razorpay test-mode credentials are wired in), don't silently accept
        # everything — fail closed so this is caught immediately, not in prod.
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
