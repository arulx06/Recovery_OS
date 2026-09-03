# RecoveryOS — Architecture

> Canonical system design. For "what is done **right now**" see `docs/CURRENT_STATE.md`. States, flows, and contracts here are derived from `backend/app/**` — if this document drifts from code, code wins and this document must be fixed.

## One-line pitch

When a Razorpay payment fails, RecoveryOS decides whether to wait for a native retry, contact the customer, send a payment link, collect a promise-to-pay, escalate — or do nothing — and measures which decisions recover the most money with the least customer friction.

## Product boundary

Razorpay Optimizer already decides **which gateway/route** should process a payment attempt. RecoveryOS starts **after** a payment has already failed or a receivable has gone overdue. It answers: *what should the business do next?* It does not touch routing.

| Capability | Razorpay already has | What RecoveryOS adds |
|------------|----------------------|----------------------|
| Payment / subscription failure events | Yes | Consumes them as recovery signals |
| Automatic subscription retries | Yes | Decides whether to stay silent while a native retry runs (`WAIT_FOR_NATIVE_RETRY`) |
| Payment Links | Yes | Decides *when* creating one is the right move |
| Gateway routing optimization | Yes (Optimizer / Smart Router) | Out of scope |
| Failure metadata (`error_source`/`step`/`reason`) | Yes | Normalizes it into 8 explicit recovery categories |
| Promise-to-pay | Suggested in brief | One action inside a larger decision controller, not the whole product |
| Stopping rules / audit trail | Required by brief | Executable, persisted, downloadable |
| Cross-intervention decisioning (wait vs contact vs link vs PTP vs escalate) | Not a standalone offering | Central policy layer of this project |

---

## System context

```mermaid
flowchart LR
    RZP[Razorpay<br/>Test Mode] -- webhook<br/>payment.failed /<br/>payment.captured /<br/>payment_link.paid /<br/>payment.dispute.created --> GW[Webhook Gateway]
    CUST[Customer<br/>inbound reply] -- POST /cases/:id/customer-reply --> GW
    GW --> ORCH[Recovery Case<br/>Orchestrator]
    ORCH --> DIAG[Failure Intelligence<br/>Engine]
    ORCH --> DISPATCH[Policy Dispatcher<br/>RECOVERY_POLICY]
    DISPATCH -- baseline/shadow/adaptive --> POL[Guardrails + Policy]
    POL -- allowed actions --> ML[Adaptive ML<br/>P*amount - cost - friction]
    ML --> POL
    POL --> DB
    DB --> RQ[Redis/RQ<br/>ID-only transport]
    RQ --> WORKER[Temporal Runtime<br/>claim / retry / reconcile]
    WORKER --> EXEC[Action Executor]
    EXEC -- simulated / Test Mode --> RZPAPI[Razorpay Payment Links API<br/>optional]
    EXEC -- stored only --> MSG[CustomerMessage<br/>no delivery transport]
    WORKER --> PTP[Linked PTP Follow-up]
    POL --> EXP[Experiment Runner<br/>baseline vs friction-aware adaptive]
    ORCH --> DB[(PostgreSQL<br/>source of truth)]
    EXP --> DB
    DB --> DASH[React Dashboard]
```

---

## Components and status

| Component | Status | Responsibility | Provider calls? |
|-----------|--------|---------------|-----------------|
| Webhook Gateway `app/routers/webhooks.py` | [IMPLEMENTED] | Verify `x-razorpay-signature` on raw bytes, require `x-razorpay-event-id`, dedupe via unique constraint, persist `PaymentEvent` | No |
| Failure Intelligence `app/services/failure_diagnosis.py` | [IMPLEMENTED] | Map (`error_source`,`error_step`,`error_reason`, subscription context) → 8 categories | No |
| Recovery Case Orchestrator `app/services/orchestrator.py` | [IMPLEMENTED] | Owns every `RevenueCase.state` mutation; routes events to diagnosis/policy/recovery/dispute/PTP | No directly — delegates |
| Guardrail Engine `app/services/policy_engine.py` | [IMPLEMENTED] | Enforce `max_contacts_per_case`, `max_contacts_per_7_days`, `min_contact_interval_hours`, `max_automated_amount`, `max_total_attempts` | No |
| Baseline Policy `app/services/policy_engine.py:decide` | [IMPLEMENTED] | Pick highest-ranked allowed action for the category (deterministic) | No |
| Policy Dispatcher `app/services/policy_dispatcher.py` | [IMPLEMENTED] | Routes `RECOVERY_POLICY=baseline/shadow/adaptive` to correct engine; shadow audits without side effect; adaptive falls back to baseline | No |
| Adaptive ML Policy `app/services/ml_policy.py:decide_ml` | [IMPLEMENTED · LIVE] | Friction-aware `P*amount - cost - friction_weight*friction`; shares guardrails; provenance + manifest fingerprint | No — model inference locally; `Razorpay` never called directly |
| Temporal Runtime `app/services/temporal_runtime.py` | [IMPLEMENTED · VERIFIED] | Atomic action claims, wait wake-up, bounded retry, stale-job checks, lease recovery, reconciliation | Delegates provider calls after claim commit |
| RQ Transport `app/services/task_queue.py`, `app/jobs.py` | [IMPLEMENTED · VERIFIED] | Publish ID-only immediate/delayed jobs after DB commit; Redis is reconstructable | Redis only |
| Action Executor `app/services/action_executor.py` | [IMPLEMENTED · VERIFIED] | Perform provider calls outside DB transactions and persist final results | Razorpay (Payment Links) and LLM (draft) when enabled |
| Razorpay Client `app/services/razorpay_client.py` | [IMPLEMENTED · SIMULATED / TEST_MODE_ONLY] | `POST /v1/payment_links`; simulation fallback; `rzp_test_*` gate | Yes — Razorpay Test Mode when `RAZORPAY_API_ENABLED=true` |
| LLM Client `app/services/llm_client.py` | [IMPLEMENTED · SIMULATED / PARTIAL] | Draft outbound text; extract PTP intent `{intent, amount, date, confidence}` | Yes — Anthropic Messages API when `LLM_API_ENABLED=true`; **anthropic-only**, other providers raise |
| PTP Extractor `app/services/ptp_extractor.py` | [IMPLEMENTED] | Validate LLM extraction before recording `PromiseToPay` | No |
| PTP Follow-up `app/services/ptp_followup.py` | [IMPLEMENTED · VERIFIED] | Resolve the exact linked promise after its merchant-local due day | No |
| Windows Worker `app/worker.py` | [IMPLEMENTED · VERIFIED] | RQ `SimpleWorker` with timer timeout, avoiding unsupported fork/SIGALRM | Redis/PostgreSQL |
| Experiment Runner `app/services/experiment_runner.py` | [IMPLEMENTED] | Matched-scenario baseline vs adaptive with `run_id` + CSV audit | No |
| Dashboard `frontend/src/**` | [IMPLEMENTED · PARTIAL] | Health, case list, experiment panel; `GET /cases/:id` exists without a rendered view | No |

Money-moving decisions are made by deterministic code (guardrails + policy), never by an LLM.

---

## Event flow

```mermaid
sequenceDiagram
    participant RZP as Razorpay
    participant GW as Webhook Gateway
    participant DB as PostgreSQL
    participant ORCH as Orchestrator
    participant DIAG as Failure Diagnosis
    participant POL as Policy + Guardrails
    participant RQ as Redis/RQ
    participant WORKER as Temporal Worker
    participant EXEC as Action Executor

    RZP->>GW: POST /webhooks/razorpay<br/>(event, payload, x-razorpay-signature, x-razorpay-event-id)
    GW->>GW: verify_signature(raw_body, secret)
    GW->>DB: INSERT PaymentEvent (unique event_id)
    alt duplicate event_id
        DB-->>GW: IntegrityError
        GW-->>RZP: 200 {ignored: duplicate_event}
    end
    GW->>ORCH: handle_event(db, event, payload)
    ORCH->>DIAG: classify_failure(entity)
    DIAG-->>ORCH: failure_category
    ORCH->>POL: decide(case) — baseline, inline
    POL->>DB: INSERT Decision + Action (SCHEDULED), set case.state
    ORCH->>DB: COMMIT
    GW->>RQ: enqueue(action_id) after commit
    GW-->>RZP: 200 {case_id, case_state}
    RQ->>WORKER: app.jobs.process_action(action_id)
    WORKER->>DB: lock case; conditional SCHEDULED → EXECUTING; COMMIT
    alt provider action
        WORKER->>EXEC: perform(snapshot), no DB transaction
        EXEC-->>WORKER: result / expected failure
    else WAIT / PTP
        WORKER->>WORKER: deterministic temporal transition
    end
    WORKER->>DB: re-lock; finalize / retry / stale no-op; COMMIT
```

---

## State ownership

- State mutation is limited to owning services: orchestrator for inbound events/replies, policy engine for decision outcomes, and temporal runtime/action executor/PTP follow-up for claimed worker transitions. Routers, queue transport, and ML code do not mutate case state.

---

## Recovery case state machine — canonical definition

Derived from `app/services/orchestrator.py:46` and `app/services/policy_engine.py:41`.

```mermaid
stateDiagram-v2
    [*] --> DETECTED : payment.failed creates case
    DETECTED --> DIAGNOSED : classify_failure
    DIAGNOSED --> DECISION_READY : guardrail check begins (ephemeral)
    DECISION_READY --> WAITING : WAIT / WAIT_FOR_NATIVE_RETRY
    DECISION_READY --> ACTION_SCHEDULED : CREATE_PAYMENT_LINK / CONTACT_CUSTOMER / COLLECT_PROMISE_TO_PAY
    DECISION_READY --> HUMAN_REVIEW : ESCALATE / failed execution / unclear PTP
    DECISION_READY --> STOPPED : STOP (max_total_attempts)
    ACTION_SCHEDULED --> AWAITING_OUTCOME : executor marks EXECUTED\n(payment link / drafted message)
    ACTION_SCHEDULED --> HUMAN_REVIEW : executor marks FAILED
    AWAITING_OUTCOME --> RECOVERED : payment.captured / payment_link.paid
    WAITING --> RECOVERED : payment.captured / payment_link.paid
    HUMAN_REVIEW --> RECOVERED : payment.captured / payment_link.paid
    ACTION_SCHEDULED --> RECOVERED : late capture (any pre-terminal except DISPUTED)
    AWAITING_OUTCOME --> DISPUTED : payment.dispute.created / customer dispute reply
    WAITING --> DISPUTED : dispute
    HUMAN_REVIEW --> DISPUTED : dispute
    RECOVERED --> DISPUTED : dispute overrides recovery (chargeback after capture)
    AWAITING_OUTCOME --> AWAITING_OUTCOME : customer promise_to_pay\n(recorded + FOLLOW_UP_PTP scheduled)
    WAITING --> DIAGNOSED : due WAIT worker re-evaluates
    AWAITING_OUTCOME --> HUMAN_REVIEW : due linked FOLLOW_UP_PTP — BROKEN promise
    STOPPED --> RECOVERED : legitimate late recovery event

    note right of WAITING : RQ delayed wake-up;\nPostgreSQL action is authoritative
    note right of AWAITING_OUTCOME : webhook / reply / linked PTP deadline
    RECOVERED --> [*]
    STOPPED --> [*]
    DISPUTED --> [*]
```

### State inventory

| State | Terminal? | How entered | In `REPLYABLE_STATES`? | Notes |
|-------|-----------|-------------|------------------------|-------|
| `DETECTED` | No | `_handle_failure` creates `RevenueCase` | No | Immediately diagnosed within same request |
| `DIAGNOSED` | No | `_diagnose` sets `failure_category` + `state=DIAGNOSED` | Yes — but only ephemeral | Immediately passed to `policy_engine.decide` |
| `DECISION_READY` | No | `policy_engine.decide` sets before choosing | Yes | Never persisted long — transitions inline to a final state |
| `WAITING` | No | `WAIT` / `WAIT_FOR_NATIVE_RETRY` | Yes | Worker wakes at `scheduled_for`; recovery/dispute may arrive first |
| `ACTION_SCHEDULED` | No | `CREATE_PAYMENT_LINK` / contact action before execution | Yes | Worker claim leads to outcome, retry, or human review |
| `AWAITING_OUTCOME` | No when linked PTP exists | Action executed or PTP recorded | Yes | Webhook/reply/PTP deadline can transition it |
| `HUMAN_REVIEW` | Automation-terminal | `ESCALATE`, exhausted execution, invalid/broken PTP | Yes | Legitimate recovery remains possible |
| `RECOVERED` | **Yes** | `_recover_case` (late capture or `payment_link.paid`) | No | Dispute still overrides to `DISPUTED` |
| `STOPPED` | **Yes** | `STOP` (stopping rule) | No | `RECOVERED` still possible via inbound recovery event; dispute can override |
| `DISPUTED` | **Yes** | `_dispute_case` | No | Always wins, even over `RECOVERED` |

### Known deviation between intended and actual transitions

- `REPLYABLE_STATES` (`orchestrator.py:294`) includes `DECISION_READY`. Since `DECISION_READY` is never persisted beyond the current transaction, this branch is effectively dead. Harmless but documented in `docs/CURRENT_STATE.md` debt.
- `WAIT` and `WAIT_FOR_NATIVE_RETRY` share execution logic but use independently configurable delays. A completed wait is blocked from repeating so re-evaluation progresses conservatively.

---

## Database as source of truth

```mermaid
erDiagram
    customers ||--o{ revenue_cases : has
    revenue_cases ||--o{ payment_events : receives
    revenue_cases ||--o{ decisions : evaluated
    revenue_cases ||--o{ actions : scheduled
    revenue_cases ||--o{ promises_to_pay : promises
    revenue_cases ||--o{ customer_messages : exchanges
    revenue_cases ||--o{ audit_events : logs
    revenue_cases ||--o{ experiment_cases : participates

    customers {
        string id PK
        string external_ref
        string contact_channel
    }
    revenue_cases {
        string id PK
        string razorpay_payment_id
        string razorpay_subscription_id
        string razorpay_payment_link_id
        decimal amount
        string failure_category
        string state
    }
    payment_events {
        string id PK
        string revenue_case_id FK
        string razorpay_event_id UK
        string event_type
        string razorpay_payment_id
        string razorpay_subscription_id
        string razorpay_payment_link_id
        json raw_payload
    }
    decisions {
        string id PK
        string revenue_case_id FK
        string chosen_action
        decimal expected_value
        json alternatives
        json guardrails_applied
        string policy_mode
        string model_version
        string model_fingerprint
        string friction_profile
        decimal friction_weight
    }
    actions {
        string id PK
        string revenue_case_id FK
        string decision_id FK
        string promise_to_pay_id FK
        string action_type
        string status
        datetime scheduled_for
        datetime executed_at
        int attempt_count
        int max_attempts
        datetime claimed_at
        datetime enqueued_at
        string queue_job_id
        text last_error
        json result
    }
    promises_to_pay {
        string id PK
        string revenue_case_id FK
        decimal promised_amount
        datetime promised_date
        string status
    }
    customer_messages {
        string id PK
        string revenue_case_id FK
        string direction
        string channel
        text body
        json extracted
    }
    audit_events {
        string id PK
        string revenue_case_id FK
        string event
        json detail
    }
    experiment_cases {
        string id PK
        string revenue_case_id FK
        string run_id
        string arm
        string failure_category
        string chosen_action
        decimal amount_at_risk
        decimal amount_recovered
    }
```

- `payment_events`, `decisions`, `audit_events` are **append-only** — never overwritten. The history is itself the product.
- Migrations own schema (`backend/alembic/versions/*`); `app/models.py` is the source of truth for model shape.
- For the full column list, read `app/models.py:29`.

### Durable action protocol

1. Request transaction persists the event, case, decision, and action.
2. After commit, `task_queue` publishes `action_id`; a publish failure leaves recoverable DB state.
3. Worker locks the case and conditionally updates a due action from `SCHEDULED` to `EXECUTING`, increments attempts, and commits.
4. Provider I/O runs against a detached snapshot with no DB transaction open.
5. Finalization re-locks state. Stale/terminal/superseded work is cancelled or ignored; expected failures retry with bounded exponential delay.
6. Reconciliation resets expired leases and republishes every scheduled action, including future jobs after Redis loss.

RQ delivery is at least once. The DB claim prevents concurrent duplicate execution.

### Provider-side Payment Link reconciliation — at-least-once execution + provider validation

```
Action SCHEDULED (reference_id = Action.id, notes={recoveryos_case_id, recoveryos_action_id})
        │
        ▼
Worker claims EXECUTING (attempt++)  ──COMMIT
        │
        ▼
Provider POST /v1/payment_links  (outside any DB transaction)
        │
   ┌────┼───────────────────────┐
   │    │                       │
 success  definite 4xx      ambiguous (timeout / network / 5xx / 429 / duplicate reference_id)
   │    │ failure               │
   │    ▼                       ▼
   │  retry → HUMAN_REVIEW   provider GET /v1/payment_links?reference_id=Action.id
   │                            │
   ▼                            ├─ found + validated (reference_id, amount, currency, notes, status not expired/cancelled)
 finalize:                      │     → canonical apply_success() → EXECUTED / AWAITING_OUTCOME, audit action_reconciled
  apply_success()               │       (reuses same DB path as normal success; result marked _reconciled=true)
  → EXECUTED                     │
  → AWAITING_OUTCOME            ├─ found but mismatch / expired / cancelled → FAILED + HUMAN_REVIEW, audit payment_link_reconciliation_failed
                                │
                                ├─ reliably absent → bounded retry (exponential), audit payment_link_reconciliation_absent
                                │
                                └─ lookup itself ambiguous → keep EXECUTING, audit pending, bounded retry later
```

- **Stable identity:** `reference_id = Action.id` (UUID, 36 chars < 40-char limit) — `razorpay_client.build_provider_reference()` is the single canonical helper. One `RevenueCase` may have many `Actions`, but one `CREATE_PAYMENT_LINK` `Action` maps to one intended provider link. Notes carry `recoveryos_case_id` + `recoveryos_action_id` for second-factor validation.
- **Provider primitive verified (official docs, Sep 2026):**
  - `POST /v1/payment_links` with `reference_id` — must be unique per link; duplicate returns `400 An existing reference id has been passed`.
  - `GET /v1/payment_links?reference_id=X` — list/filter by `reference_id` (returns `{payment_links: [...]}`); direct `GET /v1/payment_links/{id}` also exists but requires the `plink_*` id.
  - `GET /v1/payment_links?reference_id=` is the strongest safe lookup; no separate `Idempotency-Key` header exists for Payment Links.
  - Statuses `created`/`partially_paid`/`expired`/`cancelled`/`paid` — only `created`/`partially_paid`/`paid` are adoptable; `expired`/`cancelled` route to `HUMAN_REVIEW`.
- **Crash-after-accept:** `EXECUTING` lease expires → `reconcile_actions()` runs `_reconcile_stale_payment_link()` **before** generic lease reset — it does `list?reference_id` outside any transaction, validates, and either adopts (`EXECUTED`) or allows the normal `SCHEDULED` retry. No second `POST` is sent when the link already exists.
- **Timeout/unknown result:** same provider lookup before any retry; if lookup says absent, retry is safe; if lookup is itself ambiguous, the action stays `EXECUTING` and is retried later with bounded `max_attempts` (→ `payment_link_manual_review_required` when exhausted).
- **Redis loss:** PostgreSQL retains `SCHEDULED` with `last_error=provider_outcome_ambiguous`; `reconcile_actions()` republishes without immediately creating another provider link — the next worker claim will reconcile first.

The live publisher and reconciler require `RevenueCase.source=razorpay`. Experiment/synthetic actions remain offline records even though they use the same policy and table.

---

## Policy / guardrail boundary (adaptive is live — guardrails remain authoritative)

```
                        Diagnosis
                            │
                    Stopping Rule (STOP deterministic)
                            │
                        Guardrails
                            │
                    Policy Dispatcher (RECOVERY_POLICY)
                     /      |       \
              baseline   shadow   adaptive
                 │         │         │
                 │         │         ▼
                 │         │    ML + Utility
                 │         │    P*amount - cost - friction_weight*friction
                 │         │         │
                 └─────────┴─────────┘
                            │
                        Decision
                            │
                         Action
                            │
                      temporal RQ
                            │
                    side-effect safety
```

```
policy_engine.decide(case)               ml_policy.decide_ml(case)  [friction-aware]
  │  DIAGNOSED                               │  DIAGNOSED
  ▼                                         ▼
  STOP? → STOP                              STOP? → STOP (deterministic)
  │                                         │
  CATEGORY_ACTION_PREFERENCE                ALL_ACTIONS → guardrail filter → semantic filter
  │                                         (WAIT_FOR_NATIVE_RETRY only if subscription)
  filter check_action_allowed               scorer.rank_actions_with_friction
  │                                         (batched, canonical features)
  first allowed wins                        utility = p*amount - cost - weight*friction
  │                                         highest utility wins
  ▼                                         ▼
  record_decision (policy_mode=baseline)    record_decision (policy_mode=adaptive, provenance)
```

- Guardrails are applied **identically** for baseline, shadow, and adaptive — adaptive only ranks the `guardrail-allowed` set (see `policy_dispatcher` + `ml_policy._adaptive_choose`). Adaptive cannot bypass `max_contacts`, `cooldown`, `max_automated_amount`, or `max_total_attempts`; baseline fallback via `adaptive_fallback` audit never strands a case in `DIAGNOSED`.
- Shadow: baseline executes and creates `shadow_adaptive_recommendation` audit with `baseline_chosen` vs `adaptive_suggested`, `fingerprint`, `friction_profile`, `candidates`; no second Decision/Action/queue job.
- Guardrail defaults (`policy_engine.py:68`): `max_contacts_per_case=3`, `max_contacts_per_7_days=2`, `min_contact_interval_hours=12`, `max_automated_amount=₹25,000`, `max_total_attempts=5`.

---

## ML boundary (live friction-aware adaptive)

```
features.py: canonical FEATURE_COLUMNS ──────────────────┐
  (failure_category, action_taken, amount, days_overdue,  │  ONE pipeline: train, offline eval, live scoring
   previous_contacts, subscription_linked, hour, dow)     │  LIVE_AVAILABLE or DERIVABLE_FROM_HISTORY only
                                                          │
ground_truth.py: BASE_PROBABILITY[(category, action)]  ──┤
     true_probability(...) modulated by amount/overdue/    │  synthetic only — same as before
       contact fatigue/subscription                        │
                                                          │
  synthetic_history.py: generate_history(n, seed) ◄───────┤ uniform action sampling
                           │                               │
                     train.py: HistGradientBoosting ◄──────┘  CATEGORICAL: failure_category, action_taken
                              (OneHotEncoder + numeric)        NUMERIC: amount, days_overdue, ...
                           │  → artifacts/model.joblib + manifest.json  (version, fingerprint, metrics, Brier)
                           │     fingerprint = sha256(model.joblib) — provenance
                           ▼
         scorer.py: rank_actions_with_friction — batched, manifest-validated
           utility = p*amount - cost - friction_weight*friction
                           │
              friction.py: BASE_FRICTION + previous_contacts*increment
                           │  WAIT 0, WAIT_NATIVE 2, CREATE 25, CONTACT 40, PTP 60, ESCALATE 80
                           │  profiles: revenue_first(4), balanced(18, default), low_friction(45)
                           │
                  ml_policy.py: decide_ml — guarded, dispatch-aware, provenance
                           │  policy_mode, model_version, fingerprint, friction_profile in Decision
                           │
           experiment_runner.py / evaluate_policies.py — common-random-numbers, synthetic, friction metrics
```

- **One feature pipeline:** `app/ml/features.py` defines `FEATURE_COLUMNS` + `FEATURE_SCHEMA_VERSION=v1`; `train.py` imports it, `scorer.py` validates `validate_feature_row`, and `ml_policy` builds via `build_live_features` — train and live share exact names/semantics.
- **Synthetic-only scope:** `ground_truth.py` remains synthetic; training against same simulator is circular — see `docs/ML_AND_EVALUATION.md` calibration + offline-policy-evaluation limitation.
- **Live path:** `orchestrator._diagnose` → `policy_dispatcher.decide_for_case` → `RECOVERY_POLICY=baseline` (default, safe) or `adaptive` (validated manifest + batched scoring) or `shadow` (baseline executes, adaptive audited). Adaptive respects stopping rule and guardrails; `Razorpay` never called directly from policy.
- **Provenance:** `manifest.json` beside `model.joblib` (`model_version=recovery-v1`, `feature_schema_version`, `fingerprint`, `metrics` incl. Brier, `synthetic_data_notice`); `Decision.policy_mode / model_version / fingerprint / friction_*` plus `decision.alternatives[_provenance]` and `AuditEvent(adaptive_decision / adaptive_fallback / shadow_*)`.
- **Cost vs friction:** `ACTION_COST` is financial proxy (`WAIT 0 … ESCALATE 50`); `BASE_FRICTION` is customer-intervention score (dimensionless) converted via `friction_weight` (profile-dependent, INR-equivalent). They are not double-counted: guardrails block, friction ranks.
- **Calibration:** `train.py` reports `ROC-AUC 0.7756, log loss 0.4715, Brier 0.1597` on holdout; no Platt/isotonic calibration applied — synthetic holdout and GBDT native probabilities are sufficient for utility ordering (see `ML_AND_EVALUATION.md`).

---

## LLM boundary

| Concern | Truth |
|---------|-------|
| Provider | **Anthropic only** (`app/services/llm_client.py:32` `ANTHROPIC_MODEL=claude-3-5-haiku-latest`). `LLM_PROVIDER != anthropic` raises `LLMAPIError`. Not provider-agnostic. |
| Enablement | `LLM_API_ENABLED=true` **and** `LLM_API_KEY` must both be set; credentials alone never trigger network. |
| Fallback | Templated drafts (`_TEMPLATES`); regex/keyword PTP extraction (`_simulated_extract`, `DISPUTE_PHRASES`, `AMOUNT_PATTERN`). Both tag `simulated=true` / `channel=simulated`. |
| Allowed responsibilities | Draft one outbound recovery message; classify a single inbound customer reply into `{promise_to_pay, dispute, unclear}` + extract `{amount, date, confidence}`. |
| Prohibited responsibilities | Never decides whether money moves. Extraction is validated by `ptp_extractor.validate_promise` before recording; disputes still route through the same `_dispute_case` path as Razorpay webhooks. |
| Failure mode | `LLMAPIError` → executor marks `Action FAILED` + `HUMAN_REVIEW`; malformed Anthropic JSON → `unclear` with `parse_error` (does not crash pipeline). |

---

## Razorpay integration boundary

- Webhooks: `POST /webhooks/razorpay` verifies `x-razorpay-signature` on raw bytes (`app/core/security.py:11`); requires `x-razorpay-event-id`; deduped via unique DB constraint. Supported inbound events: `payment.failed`, `subscription.halted`, `payment.captured`, `subscription.charged`, `payment_link.paid`, `payment.dispute.created` (`app/services/orchestrator.py:36`).
- Payment Links: `create_payment_link` (`app/services/razorpay_client.py:48`) — **simulated** by default (`plink_sim_*`); live only when `RAZORPAY_API_ENABLED=true` with a `rzp_test_*` key. `rzp_live_*` rejected. Fulfillment is via a *different* `razorpay_payment_id` than the failed payment; matched on `RevenueCase.razorpay_payment_link_id`.
- Subscription vs receivable: `razorpay_payment_id` is the unique live receivable (partial unique index); `razorpay_subscription_id` names the billing relationship, not a specific cycle — correlation uses payment-id first and only falls back to the latest open subscription case; out-of-order recovery suppression requires an exact `payment_id` match, so a prior cycle's success does not suppress a later cycle's failure.
- **What is not implemented:** invoice/Order-level billing-cycle model, Subscriptions/Orders API, automatic HMAC key rotation, production live-money flow.

---

## Frontend boundary

- Routes: `GET /health`, `GET /cases`, `GET /cases/{id}`, `POST /cases/{id}/customer-reply`, `POST/GET /experiments`, `GET /experiments/{id}/export.csv` (`app/main.py:12` + `app/routers/*`).
- Dashboard shows: backend health, case list (state / failure category / chosen action), and the persisted experiment runner (side-by-side baseline vs adaptive, incremental ₹, action distribution, CSV audit). `GET /cases/{id}` detail (`decisions`/`actions`/`audit_trail`/`promises_to_pay`/`messages`) exists but is **not rendered** by the current frontend (`frontend/src/App.tsx:113` only lists cases).
- No authentication, no per-merchant routing, no real-time polling/websocket.

---

## External side-effect boundary

| Action type | Side effect | Transport | Delivery guarantee |
|-------------|------------|-----------|--------------------|
| `WAIT`, `WAIT_FOR_NATIVE_RETRY` | Re-evaluate baseline policy | RQ delayed job | DB claim + stale no-op; no provider call |
| `CREATE_PAYMENT_LINK` | Payment Link creation | RQ worker → Razorpay `POST /v1/payment_links` (`reference_id=Action.id`) or simulation → `GET /v1/payment_links?reference_id=` on ambiguity/stale | At-least-once worker claim + provider reconciliation + `reference_id` uniqueness; `DB claim alone cannot give exactly-once` — validated adoption (`action_reconciled`) or bounded `HUMAN_REVIEW` on mismatch; never blindly recreates |
| `CONTACT_CUSTOMER`, `COLLECT_PROMISE_TO_PAY` | Customer message drafted + stored | RQ worker; no delivery transport | Bounded draft retry; one DB finalization |
| `FOLLOW_UP_PTP` | Break exact unpaid promise after local day | RQ delayed job | DB claim + exact FK linkage |
| `ESCALATE` | None | None | Immediately `EXECUTED` + `HUMAN_REVIEW` |
| `STOP` | None | None | Immediately `EXECUTED` + `STOPPED` |

---

## Future components

- **Production Razorpay live-money path [PLANNED]:** Separate credentials, environment gate, and hardened HMAC/secret management.
- **Message delivery transport [PLANNED]:** Actual SMS/email/WhatsApp dispatch for drafted `CONTACT_CUSTOMER` messages.
- **Observability / explainability UI [PLANNED]:** Promote the minimal `GET /cases/{id}` provenance + `/health` adaptive readout into a polished dashboard (policy mode, fingerprint, friction vs revenue Pareto).

---

## Build order

Event pipeline → state machine → guardrails + baseline → real recovery action (Payment Links) → ML policy (friction-aware live via dispatcher, RECOVERY_POLICY) → LLM + Promise-to-Pay → measurement dashboard → reliability. Each stage depends on the prior one being trustworthy before the next layer is added. This is historical context for maintainers, not a reopened plan.
