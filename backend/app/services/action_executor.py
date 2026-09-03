"""External action execution, split from transaction-owned state changes."""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models import RevenueCase, Action, AuditEvent, CustomerMessage
from app.services import razorpay_client, llm_client

EXECUTABLE_ACTION_TYPES = {"CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY"}
EXPECTED_EXECUTION_ERRORS = (razorpay_client.RazorpayAPIError, llm_client.LLMAPIError)


@dataclass(frozen=True)
class CaseSnapshot:
    id: str
    amount: Decimal
    currency: str
    failure_category: str | None


def _log_audit(db: Session, case: RevenueCase, event: str, detail: dict | None = None):
    db.add(AuditEvent(revenue_case_id=case.id, event=event, detail=detail or {}))


def snapshot_case(case: RevenueCase) -> CaseSnapshot:
    return CaseSnapshot(
        id=case.id,
        amount=case.amount or Decimal("0"),
        currency=case.currency or "INR",
        failure_category=case.failure_category,
    )


def perform(action_id: str, action_type: str, case: CaseSnapshot) -> dict:
    """Perform the external operation without an open database transaction."""
    if action_type == "CREATE_PAYMENT_LINK":
        return razorpay_client.create_payment_link(
            amount_rupees=case.amount,
            currency=case.currency,
            description=f"Recovery for case {case.id} ({case.failure_category or 'unclassified'})",
            reference_id=action_id,
            notes={"recoveryos_case_id": case.id, "recoveryos_action_id": action_id},
        )
    if action_type in ("CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY"):
        return llm_client.draft_contact_message(
            action_type,
            {
                "amount": float(case.amount),
                "failure_category": case.failure_category,
                "case_id": case.id,
            },
        )
    raise ValueError(f"unsupported executable action type: {action_type}")


def apply_success(db: Session, case: RevenueCase, action: Action, result: dict) -> Action:
    """Persist a completed external operation in one short transaction."""
    now = utc_now()
    audit_event = "action_executed"
    is_reconciled = bool(result.get("_reconciled"))
    # Strip internal marker before persistence.
    persist_result = {k: v for k, v in result.items() if not k.startswith("_")}
    if action.action_type == "CREATE_PAYMENT_LINK":
        case.razorpay_payment_link_id = persist_result.get("id")
        audit_detail = {
            "action_type": action.action_type,
            "razorpay_payment_link_id": persist_result.get("id"),
            "short_url": persist_result.get("short_url"),
            "simulated": persist_result.get("simulated", False),
            "reconciled": is_reconciled,
            "reference_id": persist_result.get("reference_id"),
        }
        if is_reconciled:
            audit_event = "action_reconciled"
    else:
        db.add(CustomerMessage(
            revenue_case_id=case.id,
            direction="outbound",
            channel="simulated" if persist_result.get("simulated") else "llm",
            body=persist_result["body"],
        ))
        audit_detail = {
            "action_type": action.action_type,
            "message_preview": persist_result["body"][:120],
            "simulated": persist_result.get("simulated", False),
        }

    action.status = "EXECUTED"
    action.executed_at = now
    action.result = persist_result
    action.last_error = None
    case.state = "AWAITING_OUTCOME"
    case.updated_at = now
    _log_audit(db, case, audit_event, audit_detail)
    db.flush()
    return action


def sanitize_provider_error(exc: Exception) -> str:
    """Return sanitized error text without secrets."""
    msg = str(exc)
    # Strip any accidental credential leakage (should not occur, but be safe).
    for secret in ("rzp_test_", "rzp_live_", "sk-ant-"):
        if secret in msg.lower():
            return "external service request failed"
    # Keep concise, no headers/payload.
    return msg[:300] if len(msg) < 500 else "external service request failed"


def apply_terminal_failure(db: Session, case: RevenueCase, action: Action) -> Action:
    """Persist a sanitized final failure after the retry budget is exhausted."""
    now = utc_now()
    action.status = "FAILED"
    action.executed_at = now
    action.result = {"error": "external service request failed"}
    action.last_error = "external service request failed"
    case.state = "HUMAN_REVIEW"
    case.updated_at = now
    _log_audit(db, case, "action_execution_failed", {"action_type": action.action_type})
    db.flush()
    return action


def apply_reconciliation_failure(db: Session, case: RevenueCase, action: Action, reason: str) -> Action:
    """Provider reconciliation found a mismatch — do not create another link."""
    now = utc_now()
    action.status = "FAILED"
    action.executed_at = now
    action.result = {"error": reason[:500]}
    action.last_error = reason[:500]
    case.state = "HUMAN_REVIEW"
    case.updated_at = now
    _log_audit(
        db, case, "payment_link_reconciliation_failed",
        {"action_id": action.id, "reason": reason[:300]},
    )
    db.flush()
    return action
