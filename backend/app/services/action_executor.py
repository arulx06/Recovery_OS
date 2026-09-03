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
    """Perform the external operation without an open database transaction.

    - Razorpay Payment Links are created outside transaction.
    - LLM drafting is outside transaction with bounded timeout and deterministic fallback.
    - Never holds DB lock during network.
    """
    if action_type == "CREATE_PAYMENT_LINK":
        # First, create authoritative Payment Link (provider truth)
        link = razorpay_client.create_payment_link(
            amount_rupees=case.amount,
            currency=case.currency,
            description=f"Recovery for case {case.id} ({case.failure_category or 'unclassified'})",
            reference_id=action_id,
            notes={"recoveryos_case_id": case.id, "recoveryos_action_id": action_id},
        )
        # Then, optional message draft using placeholder substitution.
        # The authoritative short_url is substituted deterministically; LLM never invents URL.
        authoritative_url = link.get("short_url")
        draft = llm_client.draft_with_fallback(
            "CREATE_PAYMENT_LINK",
            {
                "amount": float(case.amount),
                "failure_category": case.failure_category,
                "case_id": case.id,
                "payment_link_url": authoritative_url,
                "has_payment_link": True,
            },
        )
        # Merge link + draft provenance; the provider link remains authoritative.
        link["draft_body"] = draft.get("body")
        link["draft_generation_method"] = draft.get("generation_method")
        link["draft_provider"] = draft.get("provider")
        link["draft_model"] = draft.get("model")
        link["draft_prompt_version"] = draft.get("prompt_version")
        link["draft_schema_version"] = draft.get("schema_version")
        link["draft_simulated"] = draft.get("simulated", False)
        return link

    if action_type in ("CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY"):
        # Use fallback wrapper — LLM failure becomes deterministic template, not action failure
        # This satisfies "LLM failure must not fail recovery"
        return llm_client.draft_with_fallback(
            action_type,
            {
                "amount": float(case.amount),
                "failure_category": case.failure_category,
                "case_id": case.id,
            },
        )
    raise ValueError(f"unsupported executable action type: {action_type}")


def apply_success(db: Session, case: RevenueCase, action: Action, result: dict) -> Action:
    """Persist a completed external operation in one short transaction.

    - For Payment Links, authoritative short_url is persisted.
    - For drafts, CustomerMessage is stored as DRAFT / NOT SENT with provenance.
    - Idempotency: caller ensures only EXECUTING actions reach here.
    """
    now = utc_now()
    audit_event = "action_executed"
    is_reconciled = bool(result.get("_reconciled"))
    # Strip internal marker before persistence.
    persist_result = {k: v for k, v in result.items() if not k.startswith("_")}
    if action.action_type == "CREATE_PAYMENT_LINK":
        case.razorpay_payment_link_id = persist_result.get("id")
        audit_detail = {
            "action_id": action.id,
            "action_type": action.action_type,
            "razorpay_payment_link_id": persist_result.get("id"),
            "short_url": persist_result.get("short_url"),
            "simulated": persist_result.get("simulated", False),
            "reconciled": is_reconciled,
            "reference_id": persist_result.get("reference_id"),
        }
        if is_reconciled:
            audit_event = "action_reconciled"
        # Also persist the drafted message if present (payment link draft)
        draft_body = persist_result.get("draft_body")
        if draft_body:
            authoritative_url = persist_result.get("short_url")
            draft_body = llm_client.sanitize_payment_link_body(draft_body, authoritative_url)
            persist_result["draft_body"] = draft_body
            db.add(CustomerMessage(
                revenue_case_id=case.id,
                direction="outbound",
                channel="simulated" if persist_result.get("draft_simulated") else "llm",
                body=draft_body,
                generation_method=persist_result.get("draft_generation_method") or ("deterministic" if persist_result.get("draft_simulated") else "llm"),
                llm_provider=persist_result.get("draft_provider"),
                llm_model=persist_result.get("draft_model"),
                prompt_version=persist_result.get("draft_prompt_version"),
                schema_version=persist_result.get("draft_schema_version"),
                status="DRAFT",
                extracted=None,
            ))
            audit_detail["draft_generation_method"] = persist_result.get("draft_generation_method")
            audit_detail["draft_provider"] = persist_result.get("draft_provider")
            # Audit for LLM drafting
            _log_audit(db, case, "llm_message_draft_generated" if persist_result.get("draft_generation_method") == "llm" else "llm_message_draft_fallback",
                       {"action_type": action.action_type, "generation_method": persist_result.get("draft_generation_method"), "provider": persist_result.get("draft_provider")})
        # Keep draft_body in action result for API visibility
    else:
        # CONTACT actions — persist draft as DRAFT with provenance
        generation_method = persist_result.get("generation_method") or ("deterministic" if persist_result.get("simulated") else "llm")
        # Ensure authoritative amount not invented — we already used deterministic rendering for amount in template
        # Validate no invented discount/fee/penalty if needed — trust prompt + validation
        # Check idempotency: avoid duplicate message if one already exists for this action
        existing = db.query(CustomerMessage).filter(
            CustomerMessage.revenue_case_id == case.id,
            CustomerMessage.direction == "outbound",
            CustomerMessage.body == persist_result["body"],
        ).first()
        if not existing:
            db.add(CustomerMessage(
                revenue_case_id=case.id,
                direction="outbound",
                channel="simulated" if persist_result.get("simulated") else "llm",
                body=persist_result["body"],
                generation_method=generation_method,
                llm_provider=persist_result.get("provider"),
                llm_model=persist_result.get("model"),
                prompt_version=persist_result.get("prompt_version"),
                schema_version=persist_result.get("schema_version"),
                status="DRAFT",
            ))
        audit_detail = {
            "action_type": action.action_type,
            "message_preview": persist_result["body"][:120],
            "simulated": persist_result.get("simulated", False),
            "generation_method": generation_method,
            "provider": persist_result.get("provider"),
            "model": persist_result.get("model"),
            "prompt_version": persist_result.get("prompt_version"),
        }
        if generation_method == "llm":
            _log_audit(db, case, "llm_message_draft_generated", {"action_type": action.action_type, "generation_method": generation_method, "provider": persist_result.get("provider"), "model": persist_result.get("model")})
        else:
            _log_audit(db, case, "llm_message_draft_fallback", {"action_type": action.action_type, "generation_method": generation_method})

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
