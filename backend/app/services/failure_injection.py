"""Bounded development/test failure-injection facility.

Must NEVER silently activate in production. Gated by FAILURE_INJECTION_ENABLED
and, when enabled, controlled via explicit header / test fixture — not by
default random failures.

Production behavior: with FAILURE_INJECTION_ENABLED=false, every helper is a
no-op and never injects. Tests may patch via monkeypatch or set the flag in
their isolated config.

When enabled, caller can pass X-Failure-Inject header with cases:
 - razorpay_timeout
 - razorpay_500
 - redis_unavailable
 - rq_enqueue_failure
 - worker_execution_failure
 - llm_timeout
 - llm_malformed
 - missing_ml_artifact
 - db_write_failure
"""
from app.core.config import settings

ALLOWED_INJECTIONS = {
    "razorpay_timeout",
    "razorpay_500",
    "redis_unavailable",
    "rq_enqueue_failure",
    "worker_execution_failure",
    "llm_timeout",
    "llm_malformed",
    "missing_ml_artifact",
    "db_write_failure",
}


def should_inject(kind: str, header_value: str | None = None) -> bool:
    if not settings.FAILURE_INJECTION_ENABLED:
        return False
    if not header_value:
        return False
    parts = {p.strip() for p in header_value.split(",")}
    return kind in parts


def injection_header(request_headers=None) -> str | None:
    if request_headers is None:
        return None
    # FastAPI Request headers case-insensitive
    if hasattr(request_headers, "get"):
        return request_headers.get("X-Failure-Inject") or request_headers.get("x-failure-inject")
    return None
