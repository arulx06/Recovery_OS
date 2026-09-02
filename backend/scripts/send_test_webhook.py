#!/usr/bin/env python3
"""
Simulate a signed Razorpay webhook against a locally running RecoveryOS
backend. Razorpay's test-mode webhook payloads use the same shape and
signing scheme as live ones, so this is a legitimate stand-in until real
test-mode credentials are wired in.

Usage:
    python scripts/send_test_webhook.py payment.failed --payment-id pay_abc123 --amount 5000
    python scripts/send_test_webhook.py payment.captured --payment-id pay_abc123 --amount 5000
    python scripts/send_test_webhook.py payment_link.paid --payment-link-id plink_sim_xxx --amount 5000

Requires RAZORPAY_WEBHOOK_SECRET to match whatever the running server has
configured (see backend/.env).
"""
import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.request

DEFAULT_URL = "http://localhost:8000/webhooks/razorpay"


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_payload(event_type: str, payment_id: str, amount_rupees: float, **extra) -> dict:
    entity = {
        "id": payment_id,
        "entity": "payment",
        "amount": int(round(amount_rupees * 100)),
        "currency": "INR",
        "status": event_type.split(".")[-1],
        **extra,
    }
    return {
        "entity": "event",
        "event": event_type,
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": int(time.time()),
    }


def build_payment_link_paid_payload(payment_link_id: str, amount_rupees: float, fulfilling_payment_id: str) -> dict:
    amount_paise = int(round(amount_rupees * 100))
    return {
        "entity": "event",
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {
                "entity": {"id": payment_link_id, "entity": "payment_link", "status": "paid", "amount": amount_paise}
            },
            "payment": {
                "entity": {"id": fulfilling_payment_id, "entity": "payment", "amount": amount_paise, "status": "captured"}
            },
        },
        "created_at": int(time.time()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("event_type", help="e.g. payment.failed, payment.captured, payment_link.paid")
    parser.add_argument("--payment-id", default="pay_local_test_1")
    parser.add_argument("--payment-link-id", default=None, help="required for payment_link.paid")
    parser.add_argument("--fulfilling-payment-id", default="pay_link_fulfillment_1",
                         help="the (new) payment id that paid the link, for payment_link.paid")
    parser.add_argument("--amount", type=float, default=1000.0, help="amount in rupees")
    parser.add_argument("--error-source", default="bank")
    parser.add_argument("--error-step", default="authorization")
    parser.add_argument("--error-reason", default="payment_failed")
    parser.add_argument("--event-id", default=None, help="defaults to a fresh id (set explicitly to test idempotency)")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--secret", default=os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""))
    args = parser.parse_args()

    if not args.secret:
        raise SystemExit("Set RAZORPAY_WEBHOOK_SECRET (env var or --secret) to match the running server's .env")

    if args.event_type == "payment_link.paid":
        if not args.payment_link_id:
            raise SystemExit("--payment-link-id is required for payment_link.paid")
        payload = build_payment_link_paid_payload(args.payment_link_id, args.amount, args.fulfilling_payment_id)
    else:
        extra = {}
        if args.event_type == "payment.failed":
            extra = {
                "error_source": args.error_source,
                "error_step": args.error_step,
                "error_reason": args.error_reason,
            }
        payload = build_payload(args.event_type, args.payment_id, args.amount, **extra)
    body = json.dumps(payload).encode()
    event_id = args.event_id or f"evt_local_{int(time.time() * 1000)}"

    req = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-razorpay-signature": sign(body, args.secret),
            "x-razorpay-event-id": event_id,
        },
    )
    with urllib.request.urlopen(req) as resp:
        print(f"HTTP {resp.status}")
        print(resp.read().decode())


if __name__ == "__main__":
    main()
