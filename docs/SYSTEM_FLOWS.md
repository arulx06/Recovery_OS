# RecoveryOS — System Flows

> Exact current behavior. For each flow: trigger, persisted entities, state transitions, side effects, external services, failure behavior, and current limitations.

---

## 1. `payment.failed` lifecycle (generic)

| Aspect | Detail |
|--------|--------|
| **Trigger** | `POST /webhooks/razorpay` with `event=payment.failed` or `subscription.halted`; body contains `payload.payment.entity: {id, amount (paise), error_source, error_step, error_reason, subscription_id?}` |
| **Preconditions** | Valid `x-razorpay-signature` on raw body (`app/core/security.py:11`); non-empty `event` string; `x-razorpay-event-id` present |
| **Persisted** | `PaymentEvent(razorpay_event_id, event_type, normalized payment/subscription/link IDs, raw_payload)` - immutable and unique on event ID; `AuditEvent(case_detected)` on new case |
| **Steps** | `handle_event` extracts an event-specific entity (`payment`, `subscription`, or `dispute`). Payment failures correlate by unique payment ID; subscription-only events use the latest open subscription case, so a recovered billing cycle is not reused for a new payment. New failures create and diagnose a case. Repeated active failures cancel stale scheduled work, reclassify, and create a fresh decision, except while a valid PTP is pending: diagnosis metadata is refreshed but the promise and deadline remain authoritative. `HUMAN_REVIEW` does not resume automation. The webhook commits first, then publishes ID-only jobs. |
| **State transition** | `∅ -> DETECTED -> DIAGNOSED -> DECISION_READY ->` one of `WAITING | ACTION_SCHEDULED | HUMAN_REVIEW | STOPPED`; a worker later moves executable actions to `AWAITING_OUTCOME` or retries/fails them. |
| **Side effects** | None occur inside the webhook transaction. Provider calls happen in workers after an atomic claim commit. |
| **External services** | Redis is contacted only after PostgreSQL commit. Razorpay/Anthropic are contacted only by claimed worker actions. |
| **Failure behavior** | Invalid input -> `400`; duplicate event ID -> `200 ignored`. Redis publish failure leaves the committed action `SCHEDULED`, records a sanitized enqueue failure, and is repaired by reconciliation. |
| **Current limitation** | Correlation uses exact provider IDs; malformed/nonstandard provider identifiers can still open separate cases. |

---

## 2. `card_expired` → `INVALID_INSTRUMENT` → `CREATE_PAYMENT_LINK` (normal success)

| Aspect | Detail |
|--------|--------|
| **Trigger** | `payment.failed` with `error_reason` containing `expired`/`invalid_card`/`card_blocked`/`invalid_vpa`/… (`failure_diagnosis.py:59`) → `INVALID_INSTRUMENT` |
| **Baseline policy choice** | `CATEGORY_ACTION_PREFERENCE[INVALID_INSTRUMENT] = [CREATE_PAYMENT_LINK, CONTACT_CUSTOMER, ESCALATE]` (`policy_engine.py:60`). If guardrails allow (amount ≤ 25k, contacts not throttled), `CREATE_PAYMENT_LINK` is chosen. |
| **Persisted (failure side)** | Same as flow 1, plus `Decision(chosen_action=CREATE_PAYMENT_LINK, alternatives, guardrails_applied)` + `Action(action_type=CREATE_PAYMENT_LINK, status=SCHEDULED, scheduled_for=now)` |
| **Execution** | RQ `app.jobs.process_action(action_id)`. Worker claims `SCHEDULED → EXECUTING` (commit), snapshots `{amount, currency, case_id, failure_category}`, closes txn, then `POST /v1/payment_links` with `reference_id=Action.id` + `notes={recoveryos_case_id, recoveryos_action_id}` outside any DB transaction. On `200`, sanitized result stored, `Action → EXECUTED`, `case.razorpay_payment_link_id = plink_*`, `case → AWAITING_OUTCOME`, `AuditEvent(action_executed, {reconciled:false, simulated})`. See `action_executor.perform` + `temporal_runtime._complete_external`. |
| **External services** | Razorpay Payment Links API (Test Mode only, gated) — `POST /v1/payment_links`. |
| **Failure behavior** | Definite `4xx` validation → `RazorpayAPIError` → bounded `SCHEDULED` retry (exponential), then `FAILED + HUMAN_REVIEW` with sanitized error (no secrets). Duplicate `Razorpay` redelivery is bounded by atomic claim; second delivery is `stale` no-op. |
| **Verification** | `tests/test_temporal_runtime.py:41` (duplicate delivery once), `tests/test_payment_link_reconciliation.py:18` (reference_id == Action.id) |

## 2a. `CREATE_PAYMENT_LINK` — provider timeout / ambiguous outcome

| Aspect | Detail |
|--------|--------|
| **Trigger** | Worker `POST /v1/payment_links` raises `Timeout`/`NetworkError`/`5xx`/`429` or duplicate-reference `400` — outcome unknown: Razorpay may have committed before dropping the response. |
| **Persisted before I/O** | `AuditEvent(provider_outcome_ambiguous, {action_id, error})` (outside finalize txn) |
| **Reconciliation** | Outside any long DB txn, `GET /v1/payment_links?reference_id=Action.id` (`razorpay_client.find_payment_link_for_action`). This list call is the provider-supported primitive; Razorpay enforces uniqueness on `reference_id` (duplicate create returns `400`), so `reference_id` is the stable at-least-once key. |
| **Outcomes** | **Found + validated** (reference_id, amount paise, currency, notes case/action, status not `expired`/`cancelled`) → adopt via canonical `apply_success` with `_reconciled=true` → `EXECUTED`, `AuditEvent(action_reconciled, {reconciled:true, provider_link_id})`. **Reliably absent** (empty list) → `AuditEvent(payment_link_reconciliation_absent)` → bounded retry `SCHEDULED` (exponential). **Lookup itself ambiguous** (timeout/`5xx`) → `AuditEvent(payment_link_reconciliation_pending)` → `SCHEDULED` with `last_error=provider_outcome_ambiguous`, bounded retry later. **Mismatch** (amount/currency/reference/notes/status) → `FAILED + HUMAN_REVIEW`, `payment_link_reconciliation_failed`, never silently adopted. |
| **Retry/bound** | Every ambiguous path respects `Action.max_attempts` (default 3); exhausted → `payment_link_manual_review_required` → `HUMAN_REVIEW`, never infinite polling. Provider lookup is targeted (one `GET` per ambiguous attempt, not per periodic reconciliation cycle). |
| **Verification** | `tests/test_payment_link_reconciliation.py:100` (ambiguous → adopted), `…:136` (absent → retry), `…:152` (lookup ambiguous → retry), `…:256` (mismatch → HUMAN_REVIEW) |

## 2b. `CREATE_PAYMENT_LINK` — crash after provider acceptance / stale `EXECUTING`

| Aspect | Detail |
|--------|--------|
| **Trigger** | Worker `POST` succeeded, Razorpay stored `plink_*` with `reference_id=Action.id`, but worker crashed/network dropped before `apply_success` commit. Action remains `EXECUTING` with `claimed_at` lease. |
| **Detection** | `reconcile_actions()` scans `EXECUTING` with `claimed_at ≤ now - ACTION_CLAIM_TIMEOUT_SECONDS (300s)`. For `CREATE_PAYMENT_LINK` it calls `_reconcile_stale_payment_link()` **before** generic lease reset. |
| **Reconciliation** | Same `GET /v1/payment_links?reference_id=Action.id` outside any txn, validated as above. If **found**, `AuditEvent(payment_link_reconciliation_started, {reason: stale_executing_claim})` then `_complete_external` with `_reconciled=true` → `EXECUTED`. If **absent**, falls through to generic `SCHEDULED` reset (`execution lease expired`) for a bounded retry. If **lookup ambiguous**, emits `payment_link_reconciliation_pending` and leaves `EXECUTING` lease to be retried next cycle. If **mismatch**, `payment_link_reconciliation_failed` → `HUMAN_REVIEW`. |
| **No duplicate create** | No second `POST` is sent when the provider already holds the link — the reconciling path adopts instead. |
| **Redis loss** | `SCHEDULED` rows remain `SCHEDULED` in PostgreSQL; `reconcile_actions()` republishes them via `task_queue.enqueue_action` without creating another provider link — the next worker claim will reconcile first. |
| **Verification** | `tests/test_payment_link_reconciliation.py:194` (stale adopts), `…:213` (stale absent → SCHEDULED) |

## 2c. `CREATE_PAYMENT_LINK` — provider object mismatch

| Aspect | Detail |
|--------|--------|
| **Trigger** | Reconciliation found a link for `reference_id` but `amount`/`currency`/`reference_id`/`notes` or `status` do not match the local snapshot (e.g., `plink_*` was manually edited, amount drift, currency change, `expired`/`cancelled`). |
| **Behavior** | `find_payment_link_for_action` raises `RazorpayAPIError` with mismatch description; caller (`_handle_ambiguous_payment_link` / `_reconcile_stale_payment_link`) catches and calls `action_executor.apply_reconciliation_failure()` → `FAILED`, `last_error` sanitized, `case → HUMAN_REVIEW`, `AuditEvent(payment_link_reconciliation_failed, {reason})`. Never adopted. Requires manual investigation — no automatic second `POST` until the inconsistency is understood. |
| **Verification** | `tests/test_payment_link_reconciliation.py:256` (reference mismatch), `…:271` (amount), `…:281` (currency), `…:289` (expired) |

## 2d. `CREATE_PAYMENT_LINK` → `payment_link.paid` recovery path

| Aspect | Detail |
|--------|--------|
| **Trigger** | Customer pays the `short_url`; Razorpay emits `payment_link.paid` with `payload.payment_link.entity.id = plink_*`. |
| **Persisted** | Same as flow 2 success side, plus `payment.captured` recovery: locks by `razorpay_payment_link_id`, cancels `SCHEDULED` work, marks pending promises `KEPT`, `RECOVERED`. |
| **Relationship to reconciliation** | Reconciliation adopts the `plink_*` and sets `AWAITING_OUTCOME`; `payment_link.paid` later drives `RECOVERED` idempotently. Duplicate `payment_link.paid` is harmless (ignored when `RECOVERED`/`DISPUTED`). A reconciled link that is already `paid` is still adopted — `paid` status is not treated as failure; recovery remains webhook-authoritative (no fabricated payment success from `paid` status alone). |
| **Verification** | `tests/test_payment_link_reconciliation.py:311` (webhook after reconcile), `tests/test_webhooks.py:219` |

## 2e. `CREATE_PAYMENT_LINK` — currently NOT delivered

Payment Link creation **does not deliver** the link to the customer — no SMS/email/WhatsApp. The customer pays by visiting `short_url` and Razorpay emits `payment_link.paid`. The short URL is genuine only when Test Mode is enabled and verified (`simulated=false`); otherwise it is `https://rzp.io/simulated/plink_sim_*` with `simulated=true`.

---

## 3. Authentication failure → `CONTACT_CUSTOMER`

| Aspect | Detail |
|--------|--------|
| **Trigger** | `payment.failed` where `error_reason ∈ {otp, authentication, 3ds, incorrect_pin, …}` or `error_step == payment_authentication` → `CUSTOMER_AUTHENTICATION` (`failure_diagnosis.py:52`) |
| **Baseline choice** | `CUSTOMER_AUTHENTICATION → [CONTACT_CUSTOMER, ESCALATE]` — `CONTACT_CUSTOMER` wins if guardrails allow |
| **Persisted** | `Decision(chosen_action=CONTACT_CUSTOMER)` + `Action(SCHEDULED)`. The worker later creates `CustomerMessage(direction=outbound, channel=simulated|llm, body=...)` and `AuditEvent(action_executed)`. |
| **Execution** | Same claim/commit/perform/finalize path as Payment Links. LLM drafting occurs with no open DB transaction. Simulated default returns templated text; live Anthropic returns message content. |
| **State** | `ACTION_SCHEDULED → AWAITING_OUTCOME` on success, `→ HUMAN_REVIEW` on `LLMAPIError`. |
| **Current limitation** | Draft is **stored, not delivered** — no messaging transport exists. The message is visible at `GET /cases/{id}.messages`. The PTP collection path (`COLLECT_PROMISE_TO_PAY`) uses a distinct second template. |

Verification: `tests/test_customer_reply.py:31` (`otp_incorrect` → `CONTACT_CUSTOMER` → `AWAITING_OUTCOME` with outbound message).

---

## 4. Payment recovery — `payment.captured` / `subscription.charged`

| Aspect | Detail |
|--------|--------|
| **Trigger** | `payment.captured` correlated by payment ID, or `subscription.charged` correlated by payment or subscription ID |
| **Persisted** | `PaymentEvent(revenue_case_id=case.id, …)` always; then either `AuditEvent(case_recovered_silently)` or `AuditEvent(recovery_event_ignored_terminal_state)` |
| **State** | No matching case is persisted as an unlinked normalized recovery signal keyed by `razorpay_payment_id`. If a later `payment.failed` arrives for the **same** `payment_id`, case creation links that prior recovery and goes directly to `RECOVERED` without scheduling automation (subscription id alone does not suppress — a subsequent cycle's `subscription.halted` is not considered the same receivable). For existing cases, `RECOVERED`/`DISPUTED` remain unchanged; any other state, including `STOPPED`, can recover. |
| **Current limitation** | Amount-inconsistency between the original failure and the capture is not validated; any capture for the same `razorpay_payment_id` recovers, even if `amount` differs. |

Verification: `tests/test_webhooks.py:132` (`payment.captured` with same `pay_test_003` → `RECOVERED`).

---

## 5. `payment_link.paid` handling

See flow 2 (recovery section). Distinctions from flow 4:

- Matched on `RevenueCase.razorpay_payment_link_id`, not `razorpay_payment_id`.
- Read path is `payload.payment_link.entity.id` (`orchestrator.py:68`).
- An unknown `link_id` returns `200 {case_id: null}` without storing a `revenue_case_id` on the event (`orchestrator.py:233`).
- Otherwise identical terminal logic to flow 4, including cancellation of `SCHEDULED` rows.

Verification: `tests/test_webhooks.py:237` unknown `link_id` is a noop.

---

## 6. Customer reply / Promise-to-Pay

| Aspect | Detail |
|--------|--------|
| **Trigger** | `POST /cases/{id}/customer-reply` `{body: string}` (`routers/cases.py:160`). This is transport-agnostic — the caller has already received the SMS/WhatsApp/email reply. |
| **Persisted (always)** | `CustomerMessage(direction=inbound, channel=simulated|llm, body, extracted={intent, amount, date, confidence})` — even when the case is terminal or extraction fails (audit completeness). |
| **Steps** | The router first confirms the case exists and closes that read transaction. PTP extraction then runs outside a DB transaction using merchant-local time. The case is re-read with a row lock. Valid promises supersede previous pending promises and cancel their linked follow-ups. A new `FOLLOW_UP_PTP` links by `promise_to_pay_id` and is scheduled at the exclusive end of the promise date in `MERCHANT_TIMEZONE`; commit precedes enqueue. |
| **State transitions** | `WAITING | AWAITING_OUTCOME | HUMAN_REVIEW → AWAITING_OUTCOME` (promise) or `→ DISPUTED` or `→ HUMAN_REVIEW`. Terminal `RECOVERED/STOPPED/DISPUTED` stay put but inbound message + ignored-state audit are still stored. |
| **Failure behavior** | Malformed Anthropic JSON → `PTPExtraction(intent=unclear, simulated=false, raw={parse_error})` → treated as `unclear` → `HUMAN_REVIEW` (no crash). `LLMAPIError` during extraction is rare (simulated path does not raise); if live path raises, the `POST` bubbles to `500` for the caller to retry (no `CustomerMessage` would have been created yet for that branch in the current ordering — the `extract_ptp_intent` call precedes persistence). |
| **Current limitations** | Message delivery does not exist. Bare weekdays resolve to the next occurrence; amount parsing does not support spelled-out amounts. Historical unlinked follow-ups are repaired only when exactly one pending promise makes linkage unambiguous. |

Verification: `tests/test_customer_reply.py:46` (clear promise → pending + scheduled follow-up), `...:68` (dispute immediate), `...:85` (dispute overrides pending promise), `...:98` (over-amount → `HUMAN_REVIEW`), `...:116` (reply on `RECOVERED` is recorded but ignored).

---

## 7. Dispute reply (explicit)

Same mechanism as flow 6 step 4, exposed separately because it is a terminal transition worth calling out:

- Regex/keyword list `DISPUTE_PHRASES` (`llm_client.py:35`: `already paid`, `double charge`, `fraud`, …) → `PTPExtraction(intent=dispute)`.
- Anthropic path: system prompt frames `dispute` as "charge is wrong / already paid / unauthorized" (`llm_client.py:216`).
- Effect: identical to `payment.dispute.created` (`orchestrator._handle_dispute` → `_dispute_case`), including cancellation of any `SCHEDULED FOLLOW_UP_PTP`. Dispute always wins, even over `RECOVERED`.

Verification: `tests/test_customer_reply.py:68`, `tests/test_webhooks.py:250`.

---

## 8. Experiment: baseline vs adaptive (synthetic)

| Aspect | Detail |
|--------|--------|
| **Trigger** | `POST /experiments {count: 1…5000, seed?: int}` (`routers/experiments.py:22`) or `scripts/evaluate_policies.py --count N --seed S`. Requires a trained `model.joblib`. |
| **Persisted** | `ExperimentCase × (count×2)` rows: one per scenario per arm (`baseline` / `adaptive`), grouped by `run_id=uuid4()`, keyed with `failure_category`, `chosen_action`, `amount_at_risk`, `amount_recovered`, `contacts_made`, `recovered` (`services/experiment_runner.py:95`). Each scenario also creates two transient `RevenueCase` rows (`source=experiment`, `state=DIAGNOSED`) that are immediately decided and never surfaced. |
| **Method** | For each scenario: pick a weighted `failure_category` + `amount` (`SCENARIO_PROFILES`, `AMOUNTS` — same mix as `evaluate_policies.py`); run `policy_engine.decide` and `ml_policy.decide_ml` on **identical** cloned cases at a fixed `evaluation_time=2026-01-07 12:00`; sample one outcome per distinct `chosen_action` from `ground_truth.sample_outcome` using a shared `random.Random(seed)` and an **outcome cache keyed by action** — so when both arms choose the same action they get the identical Bernoulli draw (common random numbers). Contacts counted as `1 if action ∈ CONTACT_ACTIONS else 0` (`CONTACT_ACTIONS={CONTACT_CUSTOMER, CREATE_PAYMENT_LINK, COLLECT_PROMISE_TO_PAY}`). |
| **Returned** | `summarize_run` aggregates per-arm: `amount_at_risk`, `amount_recovered`, `recovery_rate`, `contacts`, `escalations`, `action_cost_proxy` (sum of `ACTION_COST`), `realized_net_value = recovered - action_cost_proxy`, `action_distribution`; plus `incremental_recovered = adaptive.recovered - baseline.recovered` (`experiment_runner.py:114`). |
| **API surface** | `POST /experiments` (run), `GET /experiments` (recent `run_id` list), `GET /experiments/{run_id}` (summary), `GET /experiments/{run_id}/export.csv` (`arm, failure_category, chosen_action, amount_at_risk, amount_recovered, recovered, contacts_made`). See `routers/experiments.py`. |
| **Failure behavior** | No model → `503 ModelNotTrainedError` — nothing persisted. `count` out of range → `400`. |
| **Current limitations** | **Synthetic-only.** Training and evaluation share the same `ground_truth` simulator → circularity (see `docs/ML_AND_EVALUATION.md`). Evaluation draws contexts with `days_overdue=0, previous_contacts=0`; more complex per-case histories are not exercised. `run_id` UUID means results are reproducible only for a fixed `model.joblib` + `seed` pair — two separate `python -m app.ml.train` runs yield slightly different models. |

Verification: `tests/test_experiments.py:30` (2 rows per scenario), `...:37` (reproducible with same seed), `...:91` (API).

---

## 9. Current `WAIT` behavior

| Aspect | Detail |
|--------|--------|
| **Trigger** | Guardrail-filtered baseline choice `WAIT` (for `TRANSIENT_INFRASTRUCTURE`, `INSUFFICIENT_BALANCE`) or `WAIT_FOR_NATIVE_RETRY` (for `SUBSCRIPTION_PENDING_NATIVE_RETRY`). See `policy_engine.py:55`. |
| **Persisted** | `WAIT` is due after `WAIT_DELAY_SECONDS`; native retry wait uses `NATIVE_RETRY_DELAY_SECONDS`. Both set case `WAITING`. |
| **Execution** | The scheduled worker claim marks the wait `EXECUTED`, returns the case to `DIAGNOSED`, and re-runs the deterministic baseline policy. A completed wait is guardrail-blocked from repeating, so the policy advances to the next candidate or `ESCALATE`. Live adaptive ML remains uninvolved. |
| **Stale behavior** | Duplicate jobs, early jobs, terminal cases, or superseded waits are no-ops/cancelled. A new failure in an active state cancels the old scheduled wait and re-diagnoses from the new payload. |

---

## 10. Current `FOLLOW_UP_PTP` behavior

| Aspect | Detail |
|--------|--------|
| **Scheduled** | A valid promise creates a linked action due at midnight immediately after the promised merchant-local day, converted to naive UTC for the existing schema. |
| **Processing** | The common worker claim invokes `ptp_followup.complete_followup`. Only the linked `PENDING` promise becomes `BROKEN`; case becomes `HUMAN_REVIEW`. Superseded, already resolved, or terminal work is a stale no-op. |
| **Recovery handling** | Recovery before the deadline marks every pending promise `KEPT` and cancels scheduled follow-ups. A fresh promise marks older pending promises `SUPERSEDED`. |
| **Reconciliation** | `scripts/reconcile_actions.py` republishes all durable scheduled action types, not just PTP rows. `scripts/process_followups.py` remains a compatibility alias for the same operation. |

Verification: `tests/test_ptp_followup.py` (module-level coverage); `tests/test_customer_reply.py:46` records scheduling.

---

## Cross-cutting guarantees

- **Idempotency:** Webhook event IDs are unique. Action jobs contain only `action_id`; atomic `SCHEDULED -> EXECUTING` updates ensure only one worker claims a generation. Deterministic job IDs use `(action_id, attempt_count)`.
- **Audit trail:** Every mutation is recorded as an `AuditEvent` (`case_detected`, `case_diagnosed`, `decision_made`, `action_executed`, `action_execution_failed`, `case_recovered_silently`, `case_disputed`, `promise_to_pay_recorded`, `promise_broken_escalated`, `ptp_validation_failed`, …). Payloads live in `detail` JSON.
- **No external network from tests:** tests force provider and queue gates off, use a temp SQLite DB, point `REDIS_URL` at an inert local port, and deny external sockets.
- **Failure isolation:** expected provider failures retry with bounded exponential delay and sanitized persisted errors. Lease expiry is repaired by reconciliation; exhausted attempts become `FAILED + HUMAN_REVIEW`.
- **Transaction boundary:** no Redis, Razorpay, or Anthropic call is made while a production request/worker DB transaction remains open.
