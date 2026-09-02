# RecoveryOS — System Flows

> Exact current behavior. For each flow: trigger, persisted entities, state transitions, side effects, external services, failure behavior, and **current limitations**. If a transition requires `[PLANNED]` infrastructure, it is labeled as such — not documented as present.

---

## 1. `payment.failed` lifecycle (generic)

| Aspect | Detail |
|--------|--------|
| **Trigger** | `POST /webhooks/razorpay` with `event=payment.failed` or `subscription.halted`; body contains `payload.payment.entity: {id, amount (paise), error_source, error_step, error_reason, subscription_id?}` |
| **Preconditions** | Valid `x-razorpay-signature` on raw body (`app/core/security.py:11`); non-empty `event` string; `x-razorpay-event-id` present |
| **Persisted** | `PaymentEvent(razorpay_event_id, event_type, raw_payload)` — immutable, unique on `razorpay_event_id`; `AuditEvent(case_detected)` on new case |
| **Steps** | `handle_event` (`orchestrator.py:114`) → `_entity_from_payload` → `_handle_failure`:<br/>1. Lookup `RevenueCase` by `razorpay_payment_id` — if found, attach event + `_log_audit(failure_event_received)` + `_diagnose`; if not, create `RevenueCase(state=DETECTED)` + `AuditEvent(case_detected)`.<br/>2. `_diagnose` (`orchestrator.py:82`): if `state ∈ {DETECTED, DIAGNOSED}`, `classify_failure(entity)` (`failure_diagnosis.py:97`) → set `failure_category/error_source/step/reason`, `state=DIAGNOSED` → call `policy_engine.decide`.<br/>3. `policy_engine.decide` (`policy_engine.py:176`): guardrail check → `Decision` + `Action(SCHEDULED)` + `AuditEvent(decision_made)` + `state → ACTION_TO_STATE[action]`.<br/>4. If `Action` was created, `action_executor.execute` fires synchronously for executable types. |
| **State transition** | `∅ → DETECTED → DIAGNOSED → DECISION_READY →` one of `WAITING | ACTION_SCHEDULED | HUMAN_REVIEW | STOPPED`; then if executable, `ACTION_SCHEDULED → AWAITING_OUTCOME` (or `HUMAN_REVIEW` on failure). |
| **Side effects** | None for `WAIT` variants beyond state + DB rows; `CREATE_PAYMENT_LINK`/`CONTACT_CUSTOMER` execute inline (see flows 2–3). |
| **External services** | None unless executor chooses a Payment Link / LLM path (see flows 2, 6). |
| **Failure behavior** | Malformed JSON / non-object / empty event → `400` without DB write; duplicate `event_id` → `200 {ignored: duplicate_event}`; downstream exception currently bubbles to `500` for Razorpay to retry (no dead-letter queue). |
| **Current limitation** | Same `razorpay_payment_id` re-failed with a more specific `error_reason` re-diagnoses only if `state` still `DETECTED/DIAGNOSED`; parked states (`WAITING`, `AWAITING_OUTCOME`) do not get re-diagnosed. |

---

## 2. `card_expired` → `INVALID_INSTRUMENT` → `CREATE_PAYMENT_LINK` (including `payment_link.paid`)

| Aspect | Detail |
|--------|--------|
| **Trigger** | `payment.failed` with `error_reason` containing `expired`/`invalid_card`/`card_blocked`/`invalid_vpa`/… (`failure_diagnosis.py:59`) → `INVALID_INSTRUMENT` |
| **Baseline policy choice** | `CATEGORY_ACTION_PREFERENCE[INVALID_INSTRUMENT] = [CREATE_PAYMENT_LINK, CONTACT_CUSTOMER, ESCALATE]` (`policy_engine.py:60`). If guardrails allow (amount ≤ 25k, contacts not throttled), `CREATE_PAYMENT_LINK` is chosen. |
| **Persisted (failure side)** | Same as flow 1, plus `Decision(chosen_action=CREATE_PAYMENT_LINK, alternatives, guardrails_applied)` + `Action(action_type=CREATE_PAYMENT_LINK, status=SCHEDULED, scheduled_for=now)` |
| **Execution** | `action_executor._execute_create_payment_link` (`action_executor.py:56`):<br/>- If `RAZORPAY_API_ENABLED=false` (default) → `_simulated_payment_link` → `id=plink_sim_*`, `short_url=https://rzp.io/simulated/*`, `simulated=true`.<br/>- If enabled + credentials + `rzp_test_*` → `httpx.post(BASE_URL/payment_links)` (`razorpay_client.py:85`) and return `response.json()`.<br/>On success: `Action(status=EXECUTED, executed_at=now, result=response)`, `case.razorpay_payment_link_id = result.id`, `state=AWAITING_OUTCOME`, `AuditEvent(action_executed, {razorpay_payment_link_id, short_url, simulated})`.<br/>On failure: `Action(status=FAILED, result={error})`, `state=HUMAN_REVIEW` (`action_execution_failed`). |
| **Recovery: `payment_link.paid`** | `POST /webhooks/razorpay` `event=payment_link.paid` (`orchestrator.py:119`). `_handle_payment_link_paid` reads `payload.payment_link.entity.id` (not `payload.payment.id` — the fulfilling payment has a *different* id). Lookup `RevenueCase` by `razorpay_payment_link_id`. If terminal (`RECOVERED/STOPPED/DISPUTED`) → `recovery_event_ignored_terminal_state`, unchanged. Otherwise ` _recover_case(reason=payment_link_paid)` → cancel all `SCHEDULED` actions (`CANCELLED`), `state=RECOVERED`, `AuditEvent(case_recovered_silently, {reason, cancelled_actions, razorpay_payment_link_id})`. If `link_id` unknown → `200` with `case_id=null` (noop). |
| **External services** | Razorpay Payment Links API (Test Mode only, gated). Webhook for `payment_link.paid` is matched on the *link* id, not the payment id. |
| **Failure behavior** | Razorpay `HTTPErr` / non-`rzp_test_*` key → `RazorpayAPIError` → `FAILED` + `HUMAN_REVIEW` (does not leave case stuck in `ACTION_SCHEDULED`). |
| **Current limitation** | Payment Link creation **does not deliver** the link to the customer — no SMS/email/WhatsApp. The customer pays by visiting `short_url` and Razorpay emits `payment_link.paid`. The short URL is genuine only when Test Mode is enabled and verified; otherwise it is obviously simulated. |

Verification: `tests/test_webhooks.py:163` (`card_expired` → `AWAITING_OUTCOME`, `razorpay_payment_link_id` set), `tests/test_webhooks.py:219` (`payment_link.paid` → `RECOVERED`).

---

## 3. Authentication failure → `CONTACT_CUSTOMER`

| Aspect | Detail |
|--------|--------|
| **Trigger** | `payment.failed` where `error_reason ∈ {otp, authentication, 3ds, incorrect_pin, …}` or `error_step == payment_authentication` → `CUSTOMER_AUTHENTICATION` (`failure_diagnosis.py:52`) |
| **Baseline choice** | `CUSTOMER_AUTHENTICATION → [CONTACT_CUSTOMER, ESCALATE]` — `CONTACT_CUSTOMER` wins if guardrails allow |
| **Persisted** | `Decision(chosen_action=CONTACT_CUSTOMER)` + `Action(SCHEDULED)` → then execution creates `CustomerMessage(direction=outbound, channel=simulated|llm, body=…)` (`action_executor.py:125`) and an `AuditEvent(action_executed, {message_preview, simulated})`. |
| **Execution** | `action_executor._execute_contact` (`action_executor.py:100`): `llm_client.draft_contact_message(action_type, {amount, failure_category, case_id})`. Simulated default returns templated text (`llm_client.py:77` — `CONTACT_CUSTOMER` / `COLLECT_PROMISE_TO_PAY` have distinct templates); live (Anthropic) returns `response.content[*].text` from `POST https://api.anthropic.com/v1/messages`. |
| **State** | `ACTION_SCHEDULED → AWAITING_OUTCOME` on success, `→ HUMAN_REVIEW` on `LLMAPIError`. |
| **Current limitation** | Draft is **stored, not delivered** — no messaging transport exists. The message is visible at `GET /cases/{id}.messages`. The PTP collection path (`COLLECT_PROMISE_TO_PAY`) uses a distinct second template. |

Verification: `tests/test_customer_reply.py:31` (`otp_incorrect` → `CONTACT_CUSTOMER` → `AWAITING_OUTCOME` with outbound message).

---

## 4. Payment recovery — `payment.captured` / `subscription.charged`

| Aspect | Detail |
|--------|--------|
| **Trigger** | `POST /webhooks/razorpay` `event ∈ {payment.captured, subscription.charged}` with `payload.payment.entity.id == RevenueCase.razorpay_payment_id` |
| **Persisted** | `PaymentEvent(revenue_case_id=case.id, …)` always; then either `AuditEvent(case_recovered_silently)` or `AuditEvent(recovery_event_ignored_terminal_state)` |
| **State** | `_handle_recovery` (`orchestrator.py:190`): if no matching `RevenueCase` → `null` (noop — e.g., first-attempt success never opened a case); if `case.state ∈ {RECOVERED, STOPPED, DISPUTED}` → ignored. Otherwise `_recover_case`: cancel all `SCHEDULED` actions (`→ CANCELLED`), `state=RECOVERED`. |
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
| **Steps** | `orchestrator.handle_customer_reply` (`orchestrator.py:297`):<br/>1. `extracted = llm_client.extract_ptp_intent(body, now)` — simulated (keyword + regex) or Anthropic JSON.<br/>2. Store inbound `CustomerMessage`.<br/>3. If `case.state ∉ REPLYABLE_STATES` (`orchestrator.py:294` = `DECISION_READY, WAITING, ACTION_SCHEDULED, AWAITING_OUTCOME, HUMAN_REVIEW`) → `AuditEvent(customer_reply_ignored_terminal_state)`, return unchanged.<br/>4. If `extracted.intent == dispute` → `_dispute_case(reason=customer_reported_dispute, {message: body[:200]})`: cancel `SCHEDULED`, `state=DISPUTED`, `AuditEvent(case_disputed)`. **Overrides even a prior `AWAITING_OUTCOME` with pending `PromiseToPay`.**<br/>5. Else `validate_promise(extraction, outstanding_amount=case.amount, now)` (`ptp_extractor.py:38`) requires: `intent==promise_to_pay`, confidence ≥ 0.6, `amount ≤ outstanding`, `date ∈ [today, today+14d]`.<br/>  - Invalid → `state=HUMAN_REVIEW`, `AuditEvent(ptp_validation_failed, {reason, extraction})`.<br/>  - Valid → `PromiseToPay(promised_amount, promised_date, confidence, status=PENDING)`, `Action(FOLLOW_UP_PTP, SCHEDULED, scheduled_for=promised_date)`, `state=AWAITING_OUTCOME`, `AuditEvent(promise_to_pay_recorded)`. |
| **State transitions** | `WAITING | AWAITING_OUTCOME | HUMAN_REVIEW → AWAITING_OUTCOME` (promise) or `→ DISPUTED` or `→ HUMAN_REVIEW`. Terminal `RECOVERED/STOPPED/DISPUTED` stay put but inbound message + ignored-state audit are still stored. |
| **Failure behavior** | Malformed Anthropic JSON → `PTPExtraction(intent=unclear, simulated=false, raw={parse_error})` → treated as `unclear` → `HUMAN_REVIEW` (no crash). `LLMAPIError` during extraction is rare (simulated path does not raise); if live path raises, the `POST` bubbles to `500` for the caller to retry (no `CustomerMessage` would have been created yet for that branch in the current ordering — the `extract_ptp_intent` call precedes persistence). |
| **Current limitations** | Message delivery that would *trigger* this webhook does not exist (see flow 3). Day-of-week parsing (`llm_client.py:161`) resolves a bare weekday name to the **next** occurrence (Friday on a Friday → next Friday). Amount parsing supports `₹/Rs./INR` prefixes but not spelled-out amounts. Only the most recent `PENDING` promise is considered by the follow-up processor. |

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
| **Persisted** | `Action(action_type ∈ {WAIT, WAIT_FOR_NATIVE_RETRY}, status=SCHEDULED, scheduled_for=now)` + `Decision` + `case.state=WAITING`. |
| **Executed?** | No. `action_executor.EXECUTABLE_ACTION_TYPES = {CREATE_PAYMENT_LINK, CONTACT_CUSTOMER, COLLECT_PROMISE_TO_PAY}` — `WAIT` variants are no-ops for `execute` (`action_executor.py:44` returns unchanged). |
| **Woken?** | **No.** There is no queue, worker, or scheduler import in `backend/app/**`. The row stays `SCHEDULED` forever. The case leaves `WAITING` only on a later `POST /webhooks/razorpay` (`payment.captured` / `payment_link.paid` / dispute) or manual operator intervention. |
| **Planned replacement** | Temporal Recovery Runtime: when `WAITING`'s `scheduled_for` is considered elapsed, re-evaluate the case (potentially with updated context/ML). **Gated behind Redis-as-transport / DB-as-source-of-truth guarantees. Not implemented.** |

---

## 10. Current `FOLLOW_UP_PTP` behavior

| Aspect | Detail |
|--------|--------|
| **Scheduled** | Only via `handle_customer_reply` after a valid `PromiseToPay` (`orchestrator.py:363`): `Action(FOLLOW_UP_PTP, SCHEDULED, scheduled_for=promised_date)` with `promised_date` midnight UTC from the extraction's ISO date. |
| **Processing** | `ptp_followup.process_due_followups(db, now)` (`ptp_followup.py:38`): queries `Action(FOLLOW_UP_PTP, SCHEDULED, scheduled_for <= now)`; for each: mark `EXECUTED`; if `case.state ∈ {RECOVERED, STOPPED, DISPUTED}` → `already_recovered` (no state change); otherwise find the most recent `PromiseToPay(status=PENDING)` → `status=BROKEN`, `state=HUMAN_REVIEW`, `AuditEvent(promise_broken_escalated)`; counts as `broken`. |
| **Invocation** | Only by `scripts/process_followups.py` run manually or scheduled via external cron. No import in `app/main.py` or any router. |
| **Terminal handling** | If the case already reached `RECOVERED` via a later webhook / manual recovery before the follow-up runs, the promise is **not** marked `BROKEN` — it remains `PENDING` and the action is simply marked `EXECUTED` + counted as `already_recovered`. |
| **Current limitation** | `WAITING` / `AWAITING_OUTCOME` cases without a PTP have no follow-up action at all. A `FOLLOW_UP_PTP` whose `promised_date` is past due but `process_due_followups` has not yet run is indistinguishable from a pending promise that is still viable — the DB row is `SCHEDULED`, not a clock. |

Verification: `tests/test_ptp_followup.py` (module-level coverage); `tests/test_customer_reply.py:46` records scheduling.

---

## Cross-cutting guarantees

- **Idempotency:** `PaymentEvent.razorpay_event_id` unique (`alembic/versions/709600d2e2b1`). Application-level duplicate return (`webhooks.py:66`) **and** DB constraint prevent double-creation on concurrent deliveries.
- **Audit trail:** Every mutation is recorded as an `AuditEvent` (`case_detected`, `case_diagnosed`, `decision_made`, `action_executed`, `action_execution_failed`, `case_recovered_silently`, `case_disputed`, `promise_to_pay_recorded`, `promise_broken_escalated`, `ptp_validation_failed`, …). Payloads live in `detail` JSON.
- **No external network from tests:** `tests/conftest.py:56` guards `socket.connect` to loopback only; `RAZORPAY_API_ENABLED` / `LLM_API_ENABLED` forced `false` with fake keys.
- **Failure isolation:** `action_executor` failures (Razorpay / Anthropic `HTTPError`) set `Action(status=FAILED)` + `state=HUMAN_REVIEW` and flush — the original webhook still commits and returns `200` with `case_state=HUMAN_REVIEW`. The retry burden stays on Razorpay's webhook retry schedule plus human review.

