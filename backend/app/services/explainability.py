"""
Explainability & timeline read-model builders.

All functions are deterministic, read-only, derived from persisted PostgreSQL
truth. No invented values. Missing fields render as None/"unavailable".
"""
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import Action, AuditEvent, CustomerMessage, Decision, PaymentEvent, PromiseToPay, RevenueCase
from app.services import policy_engine
from app.ml.friction import BASE_FRICTION, CONTACT_INCREMENT

# ---------------------------------------------------------------------------
# Failure explanation (deterministic, no LLM)
# ---------------------------------------------------------------------------

FAILURE_CATEGORY_DETAILS: dict[str, dict[str, str]] = {
    "TRANSIENT_INFRASTRUCTURE": {
        "label": "Transient infrastructure",
        "meaning": "A temporary gateway or bank error — often retried automatically.",
        "raw_examples": "gateway_error, timeout, bank_server_error, network_error",
    },
    "SUBSCRIPTION_PENDING_NATIVE_RETRY": {
        "label": "Subscription awaiting native retry",
        "meaning": "Razorpay's own subscription retry is expected to run — waiting is safest.",
        "raw_examples": "subscription_id present, no specific error, source bank/gateway/network",
    },
    "INSUFFICIENT_BALANCE": {
        "label": "Insufficient balance",
        "meaning": "The payment failed due to insufficient funds in the customer account.",
        "raw_examples": "insufficient, low_balance, no_funds",
    },
    "CUSTOMER_AUTHENTICATION": {
        "label": "Customer authentication",
        "meaning": "Customer authentication was not completed (OTP / 3DS / PIN).",
        "raw_examples": "otp, incorrect_pin, authentication, 3ds, auth_failed",
    },
    "INVALID_INSTRUMENT": {
        "label": "Invalid instrument",
        "meaning": "The current payment instrument is no longer valid (expired card, blocked card, invalid VPA).",
        "raw_examples": "expired, invalid_card, card_blocked, invalid_vpa, restricted_card",
    },
    "MANDATE_ISSUE": {
        "label": "Mandate issue",
        "meaning": "The mandate / autopay authorization has an issue and needs customer action.",
        "raw_examples": "mandate",
    },
    "PERMANENT_HARD_FAILURE": {
        "label": "Permanent hard failure",
        "meaning": "A hard-decline with risk or fraud markers — further automation is not safe.",
        "raw_examples": "risk, fraud, blacklist, blocked_by_risk",
    },
    "UNKNOWN": {
        "label": "Unknown / unclassified",
        "meaning": "Failure could not be classified confidently — routed conservatively to human review.",
        "raw_examples": "payment_failed or empty signals",
    },
}

# Map raw reason substrings to human category for display? Instead we just expose stored error fields.


def failure_explanation(case: RevenueCase) -> dict:
    cat = case.failure_category or "UNKNOWN"
    detail = FAILURE_CATEGORY_DETAILS.get(cat, FAILURE_CATEGORY_DETAILS["UNKNOWN"])
    raw_signal = {
        "error_source": case.error_source,
        "error_step": case.error_step,
        "error_reason": case.error_reason,
    }
    # Filter nulls for display
    raw_present = {k: v for k, v in raw_signal.items() if v}
    return {
        "failure_category": cat,
        "category_label": detail["label"],
        "category_meaning": detail["meaning"],
        "raw_signal": raw_signal,
        "raw_signal_present": raw_present,
        "raw_examples": detail["raw_examples"],
    }


# ---------------------------------------------------------------------------
# Decision inspector
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def normalize_decision(decision: Decision) -> dict:
    """
    Build candidate comparison for one Decision.
    Only surfaces what was actually persisted; missing adaptive fields => None.
    """
    alternatives = decision.alternatives or {}
    # _provenance is metadata, not a candidate
    provenance = alternatives.get("_provenance") if isinstance(alternatives, dict) else None
    # Filter actions
    candidates = []
    guardrails = decision.guardrails_applied or {}
    for action, meta in alternatives.items():
        if action == "_provenance":
            continue
        if not isinstance(meta, dict):
            continue
        allowed = meta.get("allowed")
        # baseline has allowed + reason; adaptive has allowed=True + p_recovery etc
        entry = {
            "action": action,
            "allowed": allowed if allowed is not None else None,
            "blocked_reason": meta.get("reason"),
            "p_recovery": _safe_float(meta.get("p_recovery")),
            "cost": _safe_float(meta.get("cost")),
            "expected_value": _safe_float(meta.get("expected_value")),
            "expected_recovered_value": _safe_float(meta.get("expected_recovered_value")),
            "friction_score": _safe_float(meta.get("friction_score")),
            "friction_weight": _safe_float(meta.get("friction_weight")),
            "utility": _safe_float(meta.get("utility")),
            "selected": (action == decision.chosen_action),
        }
        # Derived friction_penalty for convenience (if both present)
        if entry["friction_score"] is not None and entry["friction_weight"] is not None:
            entry["friction_penalty"] = round(entry["friction_score"] * entry["friction_weight"], 2)
        else:
            entry["friction_penalty"] = None
        # Mark what is missing for UI to show "Not recorded"
        # Determine if this decision had adaptive fields at all
        if entry["p_recovery"] is None and decision.policy_mode in ("adaptive", "adaptive_fallback", "shadow"):
            # if adaptive but old decision without those fields, UI can show Not recorded
            pass
        candidates.append(entry)

    # Sort: selected first, then allowed True first, then by utility expected_value
    def sort_key(c):
        # selected first
        is_sel = 0 if c["selected"] else 1
        # allowed: allowed True before False/unavailable
        allowed_rank = 0 if c["allowed"] is True else 1
        # utility descending
        util = c["utility"]
        ev = c["expected_value"]
        score = -(util if util is not None else (ev if ev is not None else -1e9))
        return (is_sel, allowed_rank, score)

    candidates.sort(key=sort_key)

    # Determine explanation guidance
    # Baseline explanation: top-ranked allowed wins
    # Adaptive: highest utility wins
    policy_mode = decision.policy_mode or "baseline"
    is_adaptive = policy_mode == "adaptive"
    # Provide deterministic human explanation only if supported by data
    if not is_adaptive:
        # baseline: explain why selected action is highest valid
        exp = decision.explanation or "Baseline policy selected highest-ranked allowed action."
    else:
        # adaptive explanation already contains utility story
        exp = decision.explanation

    # Model provenance
    model_provenance = {
        "policy_mode": decision.policy_mode,
        "model_version": decision.model_version,
        "model_fingerprint": decision.model_fingerprint,
        "friction_profile": decision.friction_profile,
        "friction_weight": _safe_float(decision.friction_weight),
    }
    if provenance:
        # Merge persisted _provenance details (top_utility etc) without overwriting first-class
        model_provenance["_provenance"] = provenance

    # Determine if fallback
    fallback = None
    if decision.policy_mode == "adaptive_fallback":
        fallback = "Adaptive unavailable — baseline fallback executed."

    return {
        "decision_id": decision.id,
        "chosen_action": decision.chosen_action,
        "policy_mode": decision.policy_mode,
        "explanation": exp,
        "expected_value": _safe_float(decision.expected_value),
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
        "guardrails": guardrails,
        "candidates": candidates,
        "model_provenance": model_provenance,
        "fallback": fallback,
        "is_adaptive": is_adaptive,
    }


def guardrail_visibility(db: Session, case: RevenueCase, now: datetime | None = None) -> dict:
    """
    Show guardrail passes/blocks deterministically for current case state.
    Uses actual persisted contacts and attempt counts.
    """
    from app.core.time import utc_now
    from app.services.policy_engine import DEFAULT_GUARDRAILS, CONTACT_ACTIONS

    now = now or utc_now()
    config = DEFAULT_GUARDRAILS
    contacts = policy_engine.contacts_for_case(db, case)
    attempt_count = policy_engine.attempt_count(db, case)
    # Recent contacts in 7 days
    from datetime import timedelta
    recent = [c for c in contacts if c.created_at and c.created_at >= now - timedelta(days=7)]
    last_contact_at = max((c.created_at for c in contacts if c.created_at), default=None)
    cooldown_until = last_contact_at + timedelta(hours=config.min_contact_interval_hours) if last_contact_at else None
    cooldown_pass = (now >= cooldown_until) if cooldown_until else True

    # Determine per-guardrail pass/fail for display
    return {
        "max_contacts_per_case": {"limit": config.max_contacts_per_case, "used": len(contacts), "pass": len(contacts) < config.max_contacts_per_case},
        "max_contacts_per_7_days": {"limit": config.max_contacts_per_7_days, "used": len(recent), "pass": len(recent) < config.max_contacts_per_7_days},
        "cooldown": {"hours": config.min_contact_interval_hours, "pass": cooldown_pass, "next_allowed_at": cooldown_until.isoformat() if cooldown_until and not cooldown_pass else None},
        "max_automated_amount": {"limit": str(config.max_automated_amount), "amount": str(case.amount) if case.amount else None, "pass": (case.amount is None or case.amount <= config.max_automated_amount)},
        "max_total_attempts": {"limit": config.max_total_attempts, "used": attempt_count, "pass": attempt_count <= config.max_total_attempts},
        "contact_actions": list(CONTACT_ACTIONS),
    }


def friction_breakdown(db: Session, case: RevenueCase, decision: Decision | None = None) -> dict:
    """
    Explain friction contribution where available. If decision has friction data, surface it;
    otherwise compute current deterministic friction for each action.
    """
    from app.ml.friction import CONTACT_ACTIONS as FA_CONTACT
    weights_profile = decision.friction_profile if decision and decision.friction_profile else "balanced"
    weight = float(decision.friction_weight) if decision and decision.friction_weight is not None else None
    # If weight unknown (baseline), default balanced 18 for display? But we must not invent.
    # Show unavailable if not adaptive.
    breakdown = {
        "profile": weights_profile if decision and decision.policy_mode in ("adaptive", "shadow", "adaptive_fallback") else ("unavailable" if not decision or decision.policy_mode == "baseline" else weights_profile),
        "weight": weight,
        "components": [],
    }
    # Static base friction
    for action, base in BASE_FRICTION.items():
        entry = {"action": action, "base": base, "contact_increment": CONTACT_INCREMENT if action in FA_CONTACT else 0}
        # If we can compute current previous_contacts, add dynamic portion
        if action in FA_CONTACT:
            prev = len(policy_engine.contacts_for_case(db, case))
            entry["previous_contacts"] = prev
            entry["total_friction"] = base + prev * CONTACT_INCREMENT
            entry["friction_penalty"] = round(entry["total_friction"] * weight, 2) if weight is not None else None
        else:
            entry["previous_contacts"] = None
            entry["total_friction"] = base
            entry["friction_penalty"] = round(base * weight, 2) if weight is not None else None
        if decision is None or decision.policy_mode == "baseline":
            entry["friction_penalty"] = None
        breakdown["components"].append(entry)
    # If decision had actual per-candidate friction, prefer that for chosen action
    if decision and decision.alternatives:
        alts = decision.alternatives
        chosen = decision.chosen_action
        chosen_meta = alts.get(chosen, {}) if isinstance(alts, dict) else {}
        if "friction_score" in chosen_meta:
            breakdown["chosen_action_actual"] = {
                "action": chosen,
                "friction_score": chosen_meta.get("friction_score"),
                "friction_weight": chosen_meta.get("friction_weight"),
                "utility": chosen_meta.get("utility"),
                "expected_recovered_value": chosen_meta.get("expected_recovered_value"),
            }
    return breakdown


# ---------------------------------------------------------------------------
# Timeline builder — derived only from persisted entities
# ---------------------------------------------------------------------------

def _format_ts(dt: datetime | None) -> str | None:
    if not dt:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def is_action_reconciled(action: Action, audit_events: list[AuditEvent]) -> bool:
    """Return durable reconciliation evidence associated with this Action only."""
    action_link_id = (action.result or {}).get("id")
    for event in audit_events:
        if event.event != "action_reconciled" or not isinstance(event.detail, dict):
            continue
        audit_action_id = event.detail.get("action_id")
        if audit_action_id is not None:
            if str(audit_action_id) == str(action.id):
                return True
            # An explicit Action identity is authoritative; do not fall through
            # to a possibly inconsistent link identifier on the same event.
            continue
        audit_link_id = event.detail.get("razorpay_payment_link_id")
        if action_link_id and audit_link_id == action_link_id:
            return True
    return False

def build_timeline(
    case: RevenueCase,
    decisions: list[Decision],
    actions: list[Action],
    messages: list[CustomerMessage],
    promises: list[PromiseToPay],
    audit_events: list[AuditEvent],
    payment_events: list[PaymentEvent] | None = None,
) -> list[dict]:
    events: list[dict] = []

    # PaymentEvents (if provided)
    if payment_events:
        for pe in payment_events:
            ts = pe.received_at
            title = pe.event_type
            desc_map = {
                "payment.failed": "Payment failed — case opened or updated",
                "subscription.halted": "Subscription halted",
                "payment.captured": "Provider confirmed payment captured — recovery",
                "subscription.charged": "Subscription charged — recovery",
                "payment_link.paid": "Payment link paid — recovery",
                "payment.dispute.created": "Dispute created",
            }
            desc = desc_map.get(pe.event_type, f"Webhook received: {pe.event_type}")
            events.append({
                "timestamp": _format_ts(ts),
                "type": "payment_event",
                "title": title,
                "description": desc,
                "category": "provider",
                "metadata": {
                    "razorpay_event_id": pe.razorpay_event_id,
                    "razorpay_payment_id": pe.razorpay_payment_id,
                    "razorpay_payment_link_id": pe.razorpay_payment_link_id,
                    "event_type": pe.event_type,
                },
                "severity": "info" if pe.event_type in ("payment.failed",) else ("success" if pe.event_type in ("payment.captured", "subscription.charged", "payment_link.paid") else "warning"),
            })

    # Decisions
    for d in decisions:
        events.append({
            "timestamp": _format_ts(d.created_at),
            "type": "decision",
            "title": f"Decision: {d.chosen_action or '—'}",
            "description": d.explanation or f"Policy {d.policy_mode or 'baseline'} chose {d.chosen_action}",
            "category": "decision",
            "metadata": {
                "decision_id": d.id,
                "policy_mode": d.policy_mode,
                "chosen_action": d.chosen_action,
                "model_version": d.model_version,
                "model_fingerprint": d.model_fingerprint,
                "friction_profile": d.friction_profile,
                "friction_weight": float(d.friction_weight) if d.friction_weight is not None else None,
            },
            "severity": "info",
        })

    # Actions
    for a in actions:
        status_label = a.status
        reconciled = is_action_reconciled(a, audit_events) if a.action_type == "CREATE_PAYMENT_LINK" else False
        # Determine title
        if a.action_type in ("WAIT", "WAIT_FOR_NATIVE_RETRY"):
            if a.status == "EXECUTED":
                title = f"{a.action_type} completed — re-evaluating"
            elif a.status == "CANCELLED":
                title = f"{a.action_type} cancelled (superseded or terminal)"
            else:
                title = f"{a.action_type} scheduled"
        elif a.action_type == "CREATE_PAYMENT_LINK":
            if a.status == "EXECUTED":
                simulated = (a.result or {}).get("simulated") if a.result else None
                title = f"Payment Link {'reconciled' if reconciled else 'created'} ({'SIMULATED' if simulated else 'provider'})" if simulated is not None else f"Payment Link {a.status.lower()}"
            else:
                title = f"Payment Link {a.status.lower()}"
        elif a.action_type == "FOLLOW_UP_PTP":
            title = f"Follow-up PTP {a.status.lower()}"
        else:
            title = f"{a.action_type} {a.status.lower()}"
        desc = f"Action {a.action_type} — {a.status}"
        if a.scheduled_for:
            desc += f" scheduled for {a.scheduled_for.isoformat()}"
        if a.last_error:
            desc += f" — {a.last_error[:120]}"
        # Use scheduled_for or created_at or executed_at as timestamp priority
        ts = a.executed_at or a.scheduled_for or a.created_at
        # For SCHEDULED, use created_at or scheduled_for
        if a.status == "SCHEDULED":
            ts = a.created_at or a.scheduled_for
        elif a.status == "EXECUTING":
            ts = a.claimed_at or a.created_at
        events.append({
            "timestamp": _format_ts(ts),
            "type": "action",
            "title": title,
            "description": desc,
            "category": "temporal",
            "metadata": {
                "action_id": a.id,
                "action_type": a.action_type,
                "status": a.status,
                "scheduled_for": _format_ts(a.scheduled_for),
                "executed_at": _format_ts(a.executed_at),
                "claimed_at": _format_ts(a.claimed_at),
                "attempt_count": a.attempt_count,
                "max_attempts": a.max_attempts,
                "result": a.result,
                "last_error": a.last_error,
                "promise_to_pay_id": a.promise_to_pay_id,
                "reconciled": reconciled if a.action_type == "CREATE_PAYMENT_LINK" else None,
            },
            "severity": "info" if a.status in ("SCHEDULED", "EXECUTING") else ("success" if a.status == "EXECUTED" else ("warning" if a.status == "CANCELLED" else "error")),
        })

    # Customer messages
    for m in messages:
        if m.direction == "outbound":
            events.append({
                "timestamp": _format_ts(m.created_at),
                "type": "customer_message",
                "title": f"Outbound draft ({m.generation_method or 'deterministic'}) — DRAFT / NOT SENT",
                "description": m.body[:200] if m.body else "Draft generated",
                "category": "customer",
                "metadata": {
                    "message_id": m.id,
                    "direction": m.direction,
                    "channel": m.channel,
                    "generation_method": m.generation_method,
                    "llm_provider": m.llm_provider,
                    "llm_model": m.llm_model,
                    "prompt_version": m.prompt_version,
                    "status": m.status,
                    "body": m.body,
                },
                "severity": "info",
            })
        else:
            # inbound
            intent = (m.extracted or {}).get("intent") if m.extracted else None
            title = f"Customer replied ({intent or 'message received'})"
            events.append({
                "timestamp": _format_ts(m.created_at),
                "type": "customer_reply",
                "title": title,
                "description": m.body[:200] if m.body else "Customer reply stored",
                "category": "customer",
                "metadata": {
                    "message_id": m.id,
                    "direction": m.direction,
                    "body": m.body,
                    "extracted": m.extracted,
                    "generation_method": m.generation_method,
                    "prompt_version": m.prompt_version,
                },
                "severity": "info",
            })

    # Promises
    for p in promises:
        events.append({
            "timestamp": _format_ts(p.created_at),
            "type": "promise_to_pay",
            "title": f"Promise-to-Pay {p.status} — ₹{p.promised_amount} by {p.promised_date.isoformat() if p.promised_date else '—'}",
            "description": f"PTP {p.status} via {p.extraction_method or 'unknown'} — confidence {p.confidence}",
            "category": "ptp",
            "metadata": {
                "promise_id": p.id,
                "promised_amount": float(p.promised_amount) if p.promised_amount is not None else None,
                "promised_date": p.promised_date.isoformat() if p.promised_date else None,
                "status": p.status,
                "extraction_method": p.extraction_method,
                "confidence": float(p.confidence) if p.confidence is not None else None,
                "source_message_id": p.source_message_id,
                "prompt_version": p.prompt_version,
                "amount_method": p.amount_method,
            },
            "severity": "success" if p.status == "KEPT" else ("warning" if p.status == "BROKEN" else "info"),
        })

    # Audit events — map many to timeline but avoid duplication where already covered by other entities
    # We still include distinct audit types that represent system-level transitions not otherwise captured
    AUDIT_TITLE_MAP = {
        "case_detected": "Case detected",
        "case_diagnosed": "Failure diagnosed",
        "decision_made": "Decision recorded",
        "adaptive_decision": "Adaptive decision recorded",
        "adaptive_fallback": "Adaptive fallback → baseline",
        "shadow_adaptive_recommendation": "Shadow adaptive recommendation",
        "shadow_adaptive_error": "Shadow adaptive error",
        "action_claimed": "Action claimed by worker",
        "action_executed": "Action executed",
        "action_reconciled": "Action reconciled with provider",
        "action_retry_scheduled": "Action retry scheduled",
        "action_execution_failed": "Action failed → human review",
        "stale_action_cancelled": "Stale action cancelled",
        "case_recovered_silently": "Case recovered (provider truth)",
        "recovery_event_ignored_terminal_state": "Recovery event ignored (already terminal)",
        "case_disputed": "Case disputed",
        "payment_link_reconciliation_started": "Payment Link reconciliation started",
        "payment_link_reconciliation_absent": "Payment Link not found — retry scheduled",
        "payment_link_reconciliation_pending": "Payment Link reconciliation pending (ambiguous)",
        "payment_link_reconciliation_failed": "Payment Link reconciliation failed → human review",
        "payment_link_manual_review_required": "Payment Link manual review required",
        "provider_outcome_ambiguous": "Provider outcome ambiguous",
        "payment_link_reconciliation_retry_scheduled": "Reconciliation retry scheduled",
        "action_claim_recovered": "Expired claim recovered",
        "action_enqueue_failed": "Queue publish failed (recovered via DB)",
        "wait_completed": "Wait completed — re-evaluating",
        "ptp_extraction_completed": "PTP extraction completed",
        "ptp_extraction_uncertain": "PTP extraction uncertain",
        "ptp_validation_failed": "PTP validation failed → human review",
        "ptp_validation_rejected": "PTP rejected → human review",
        "promise_to_pay_recorded": "Promise-to-Pay recorded — follow-up scheduled",
        "customer_payment_claim_received": "Customer payment claim → disputed",
        "failure_event_received": "Repeated failure received",
        "failure_diagnosed_pending_promise": "Failure re-diagnosed (pending promise held)",
        "failure_event_ignored_terminal_state": "Failure ignored (terminal state)",
        "customer_reply_ignored_terminal_state": "Customer reply ignored (terminal state)",
        "llm_message_draft_generated": "LLM message draft generated",
        "llm_message_draft_fallback": "Deterministic draft used (LLM fallback)",
    }

    for ae in audit_events:
        # Avoid duplicating decision_made if we already have Decision entry; but keep it as richer audit
        # We'll include most audit events for completeness, marked as audit
        title = AUDIT_TITLE_MAP.get(ae.event, ae.event.replace("_", " ").title())
        # Derive severity
        if ae.event in ("case_recovered_silently", "action_executed", "action_reconciled", "wait_completed", "promise_to_pay_recorded"):
            sev = "success"
        elif ae.event in ("case_disputed", "action_execution_failed", "payment_link_reconciliation_failed", "payment_link_manual_review_required", "ptp_validation_failed"):
            sev = "warning"
        elif ae.event in ("adaptive_fallback", "action_enqueue_failed", "provider_outcome_ambiguous"):
            sev = "warning"
        else:
            sev = "info"
        # Description from detail
        detail = ae.detail or {}
        desc = ""
        if isinstance(detail, dict):
            # Short summary
            if ae.event == "case_diagnosed":
                desc = f"Diagnosed as {detail.get('failure_category')}"
            elif ae.event == "decision_made":
                desc = f"Chose {detail.get('chosen_action')} → {detail.get('resulting_state')}"
            elif ae.event == "shadow_adaptive_recommendation":
                baseline = detail.get("baseline_chosen")
                adaptive = detail.get("adaptive_suggested")
                disagreement = detail.get("disagreement")
                desc = f"Baseline {baseline} vs adaptive {adaptive}" + (" — DISAGREEMENT" if disagreement else "")
            elif ae.event == "adaptive_decision":
                desc = f"Adaptive chose {detail.get('chosen_action') or detail.get('fingerprint') or ''}".strip()
            elif ae.event == "adaptive_fallback":
                desc = f"Fallback reason: {detail.get('reason','')[:200]}"
            elif ae.event == "payment_link_reconciliation_failed":
                desc = f"Reason: {detail.get('reason','')[:200]}"
            elif ae.event == "case_recovered_silently":
                desc = f"Reason: {detail.get('reason')} — {detail.get('cancelled_actions',0)} actions cancelled"
            elif ae.event == "promise_to_pay_recorded":
                desc = f"₹{detail.get('amount')} by {detail.get('promised_date')} — follow-up {detail.get('followup_at_utc')}"
            else:
                # Generic: first 200 chars of json detail
                try:
                    import json as _json
                    desc = _json.dumps(detail)[:220]
                except Exception:
                    desc = str(detail)[:220]
        events.append({
            "timestamp": _format_ts(ae.created_at),
            "type": "audit",
            "title": title,
            "description": desc,
            "category": "audit",
            "metadata": {
                "audit_event": ae.event,
                "detail": ae.detail,
            },
            "severity": sev,
        })

    # Sort chronologically; stable sort with created fallback
    def ts_key(e):
        t = e["timestamp"]
        if not t:
            return "9999-12-31"
        return t
    events.sort(key=ts_key)
    return events

# ---------------------------------------------------------------------------
# Provider truth helpers
# ---------------------------------------------------------------------------

def provider_truth_summary(case: RevenueCase, actions: list[Action], audit_events: list[AuditEvent]) -> dict:
    # Find latest CREATE_PAYMENT_LINK action
    pl_actions = [a for a in actions if a.action_type == "CREATE_PAYMENT_LINK"]
    latest = max(pl_actions, key=lambda a: (a.created_at or datetime.min, str(a.id))) if pl_actions else None
    if not latest:
        return {"has_payment_link": False, "payment_link_id": case.razorpay_payment_link_id, "status": "none"}

    result = latest.result or {}
    action_link_id = result.get("id")
    simulated = result.get("simulated")
    short_url = result.get("short_url")
    reconciled = is_action_reconciled(latest, audit_events)
    # Determine test mode vs simulated
    # If simulated is None and no provider fields, unavailable
    mode_label = None
    if simulated is True:
        mode_label = "SIMULATED LINK"
    elif simulated is False:
        mode_label = "RAZORPAY TEST MODE"
    else:
        mode_label = "unavailable"

    return {
        "has_payment_link": bool(action_link_id),
        "latest_action_id": latest.id,
        "payment_link_id": action_link_id,
        "reference_id": result.get("reference_id") or latest.id,
        "short_url": short_url,
        "simulated": simulated,
        "mode_label": mode_label,
        "reconciled": reconciled,
        "status": latest.status,
        "amount_paise": result.get("amount"),
        "currency": result.get("currency"),
        "result": result,
    }
