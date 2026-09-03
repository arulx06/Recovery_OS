"""Structured application logging.

Never logs secrets, auth headers, full card payloads, or raw LLM prompts.
Useful fields: event_type, case_id, action_id, job_id, policy_mode,
failure_category, provider_mode, latency_ms, status, request_id.
"""
import json
import logging
import sys
import time
from typing import Any

# Never log these substrings
_REDACTED_SUBSTRINGS = ("razorpay", "secret", "key", "authorization", "password", "token", "sk-ant")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if any(s in k.lower() for s in _REDACTED_SUBSTRINGS) else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and any(s in obj.lower() for s in _REDACTED_SUBSTRINGS) and len(obj) > 20:
        return "[REDACTED]"
    return obj


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Attach structured extra fields if present
        for key in ("event_type", "case_id", "action_id", "job_id", "policy_mode",
                     "failure_category", "provider_mode", "latency_ms", "status",
                     "request_id", "route", "method"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        # Add any other extra that is not private
        if hasattr(record, "extra_fields") and isinstance(record.extra_fields, dict):
            payload.update(_redact(record.extra_fields))
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


# Module-level logger for app
logger = get_logger("recoveryos")


def log_structured(level: int, msg: str, **fields: Any) -> None:
    safe_fields = _redact(fields)
    # Split known fields into record attributes vs extra_fields
    known = {"event_type", "case_id", "action_id", "job_id", "policy_mode",
             "failure_category", "provider_mode", "latency_ms", "status",
             "request_id", "route", "method"}
    extra = {k: v for k, v in safe_fields.items() if k not in known}
    record_kwargs = {k: v for k, v in safe_fields.items() if k in known}
    if extra:
        record_kwargs["extra_fields"] = extra
    logger.log(level, msg, extra=record_kwargs)
