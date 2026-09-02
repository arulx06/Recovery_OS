# RecoveryOS — Architecture

## One-line pitch

When a Razorpay payment fails, RecoveryOS decides whether to wait, retry,
contact the customer, send a payment link, collect a promise-to-pay,
escalate — or do nothing — and measures which decisions recover the most
money with the least customer friction.

## Product boundary

Razorpay Optimizer already decides **which gateway/route** should process a
payment attempt. RecoveryOS starts **after** a payment has already failed or
a receivable has gone overdue, and answers a different question: *what
should the business do next?* We don't touch routing.

## Pipeline

```
Razorpay Test Mode (payments / subscriptions)
              │ webhooks
              ▼
┌───────────────────────────────────────────────────────────┐
│                     FastAPI application                    │
│                                                              │
│  Webhook Gateway (verify signature, dedupe, order events)  │
│                │                                             │
│                ▼                                             │
│  Recovery Case Orchestrator ──▶ Failure Intelligence Engine │
│                │                                             │
│                ▼                                             │
│  Guardrail Engine (allowed actions, contact limits,         │
│                     cooldowns, stopping rules)               │
│                │                                             │
│                ▼                                             │
│  Recovery Policy Engine (P(recovery | action, context),     │
│                            expected value)                   │
│                │                                             │
│                ▼                                             │
│  Action Executor ─┬─ WAIT                                    │
│                    ├─ CREATE_PAYMENT_LINK (Razorpay)          │
│                    ├─ CONTACT_CUSTOMER (LLM dialogue /        │
│                    │                    PTP extraction)       │
│                    ├─ ESCALATE                                │
│                    └─ STOP                                    │
└───────────────────────────────────────────────────────────┘
              │                              │
              ▼                              ▼
        PostgreSQL                     Redis + RQ worker
   (cases, events, decisions,        (scheduled/delayed actions,
    actions, audit trail)             re-evaluation timers)
              │
              ▼
   Measurement / Attribution Engine
   (₹ at risk, ₹ recovered, baseline vs. adaptive policy,
    contacts/recovery, stopping-rule activations)
              │
              ▼
      React merchant dashboard
```

Money-moving decisions are made by deterministic code (guardrails + policy
engine), never by an LLM directly. The LLM is used only where language
understanding is genuinely the job: turning a customer's free-text reply
into a structured promise-to-pay, or drafting a compliant outbound message.

## Recovery case state machine

```
DETECTED → DIAGNOSED → DECISION_READY ─┬─ WAITING (re-evaluate later)
                                        ├─ ACTION_SCHEDULED → ACTION_EXECUTED → AWAITING_OUTCOME
                                        ├─ HUMAN_REVIEW
                                        ├─ DISPUTED
                                        ├─ STOPPED
                                        └─ RECOVERED
```

All transitions go through the orchestrator — nothing else is allowed to
mutate `revenue_cases.state` directly.

## Layer responsibilities

| Layer | Input | Responsibility | AI? |
|---|---|---|---|
| Event Normalizer | Razorpay webhook | Canonical internal event format | No |
| Failure Diagnoser | error reason/source/step, history | Classify failure type | Mostly deterministic |
| Guardrail Engine | case + merchant policy | Filter to legally/business-allowed actions | No |
| Recovery Model | context + candidate actions | Estimate P(recovery) / expected value | ML |
| Policy Engine | predictions + guardrails | Pick the best allowed action | Algorithmic |
| Communication Agent | chosen action + context | Draft customer-facing message | LLM |
| PTP Extractor | customer reply | Extract `{amount, date, confidence}` | LLM (structured output) |
| Outcome Engine | payment events | Determine whether recovery succeeded | No |
| Attribution Engine | action/outcome history | Compute recovered money, experiment metrics | No |
| Explanation Layer | model/rules/decision | Human-readable "why this action" | Deterministic + ML explanation |

## Data model (Day 1 schema lock)

`customers → revenue_cases → {payment_events, decisions, actions,
promises_to_pay, customer_messages, audit_events}`, plus a standalone
`experiment_cases` table for the baseline-vs-RecoveryOS batch evaluation.

`payment_events`, `decisions`, and `audit_events` are **append-only** — the
historical sequence of what happened and why is itself the product (it's
what the explainability dashboard and the audit trail are built from).

See `backend/app/models.py` for the authoritative column-level definitions.

## What Razorpay already provides vs. what RecoveryOS adds

| Capability | Razorpay already has it | What RecoveryOS adds |
|---|---|---|
| Payment/subscription failure events | Yes | Consumes them as recovery signals |
| Automatic subscription retries | Yes | Decides whether to stay silent while native retry runs |
| Payment Links | Yes | Decides *when* creating one is the right move |
| Gateway routing optimization | Yes (Optimizer / Smart Router) | Explicitly out of scope — we start after a failure |
| Failure metadata (`error_source`/`step`/`reason`) | Yes | Converts it into an explicit recovery-action taxonomy |
| Promise-to-pay | Suggested direction in the brief | One action inside a larger decision controller, not the whole product |
| Stopping rules / audit trail | Required by the brief | Made executable, visible, and measurable |
| Cross-intervention decisioning (wait vs. contact vs. link vs. PTP vs. escalate) | Not a standalone offering | The central policy layer of this project |

## Build order rationale (why this phase order)

Event pipeline → state machine → guardrails → a working recovery action →
measurement → ML policy → LLM → polish. Each phase depends on the one
before it being trustworthy: there's no point scoring an ML policy against
input/state semantics that aren't correct yet, and there's no point
measuring "money recovered" before the policy is stable. See `README.md`
for the full 14-day phase table.
