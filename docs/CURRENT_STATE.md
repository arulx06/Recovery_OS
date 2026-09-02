# RecoveryOS — Current State

**Canonical living document for "what exists RIGHT NOW." Future implementation phases must update this file before any other documentation.**

> Status legend — see [Documentation Status Language](#documentation-status-language).

---

## Repository baseline

| Property | Value |
|----------|-------|
| Branch | `feat/recoveryos-live-runtime` |
| Baseline ancestry | Verified hardening baseline (`chore: harden RecoveryOS baseline`, `chore: establish verified RecoveryOS baseline`) |
| Backend tests | 233 passed (hermetic, `sqlite://` temp DB, external network denied) |
| Frontend `npm run build` | PASS |
| Frontend `npm run lint` (`oxlint`) | PASS |
| Database | PostgreSQL (required); SQLite for tests/CI |
| Redis/RQ | Dependency reserved — not wired into application runtime (see Capability Matrix) |
| Model artifact | `backend/app/ml/artifacts/model.joblib` — gitignored, reproduced via `python -m app.ml.train` |

> Do not hardcode a commit hash here. The branch name is the stable reference. If a specific hash must be cited for a report, add it locally and do not commit it.

### How this branch was verified

- `python -m pytest -q` → 233 passed with socket-denial guard and fake credentials (`conftest.py:56`).
- `npm run build` and `npm run lint` pass on Node 22 / Vite 8.
- `alembic upgrade head` applies cleanly on SQLite (`ci.yml`) and on a local Postgres `recoveryos` DB.
- Manual Razorpay TEST MODE path verified independently: `payment.failed` (`card_expired`) → `INVALID_INSTRUMENT` → `CREATE_PAYMENT_LINK` → `plink_*` via `https://api.razorpay.com/v1/payment_links` → `rzp.io` short URL, with `Action.result.simulated == false` when `RAZORPAY_API_ENABLED=true` and `rzp_test_*` credentials are present.

---

## Capability matrix

| Capability | Status | Evidence | Limitations |
|------------|--------|----------|-------------|
| Webhook signature verification (HMAC-SHA256 on raw body) | **IMPLEMENTED / VERIFIED** | `app/core/security.py:11`, `app/routers/webhooks.py:39`, `tests/test_webhooks.py:35` | Fails closed when `RAZORPAY_WEBHOOK_SECRET` empty; tests cover malformed JSON, non-object body, missing `x-razorpay-event-id` |
| Idempotent ingestion (`payment_events.razorpay_event_id` unique) | **IMPLEMENTED / VERIFIED** | `app/routers/webhooks.py:64` (`IntegrityError` → `duplicate_event`), migration `709600d2e2b1`, `tests/test_webhooks.py:115` | Returns 200 on duplicate; does not expose a retry queue |
| Failure diagnosis (deterministic taxonomy, 8 categories) | **IMPLEMENTED / VERIFIED** | `app/services/failure_diagnosis.py:43`, `tests/test_failure_diagnosis.py` | 8 fixed categories; anything unmatched → `UNKNOWN` → conservative `ESCALATE`; see `docs/SYSTEM_FLOWS.md` |
| Case state machine (10 states, guarded transitions) | **IMPLEMENTED** | `app/services/orchestrator.py:66`, `app/services/policy_engine.py:41`, `tests/test_policy_engine.py` | `DECISION_READY` is ephemeral; no automatic `WAITING` re-evaluation; dispute can override `RECOVERED` (by design) |
| Baseline guardrail + policy engine | **IMPLEMENTED / VERIFIED** | `app/services/policy_engine.py:176`, `tests/test_policy_engine.py` | `max_contacts_per_case=3`, `max_contacts_per_7_days=2`, `min_contact_interval_hours=12`, `max_automated_amount=₹25,000`, `max_total_attempts=5`; contact limits use `Action` history |
| Deterministic guardrails around adaptive policy | **IMPLEMENTED** | `app/services/ml_policy.py:71` reuses `policy_engine.check_action_allowed` + `record_decision` | Adaptive scorer never bypasses guardrails |
| Payment Link creation — simulated | **IMPLEMENTED / VERIFIED** | `app/services/razorpay_client.py:65`, `tests/test_razorpay_client.py:35`, `tests/test_webhooks.py:176` | Default; `id=plink_sim_*`, `short_url=https://rzp.io/simulated/*`, `simulated=true` |
| Payment Link creation — Razorpay Test Mode live call | **TEST_MODE_ONLY / VERIFIED** | `app/services/razorpay_client.py:48` (`httpx.post` to `https://api.razorpay.com/v1/payment_links`), `tests/test_razorpay_client.py:53` | Gated by `RAZORPAY_API_ENABLED=true` **and** `rzp_test_*` key; live-mode keys rejected; verified manually to return genuine `plink_*` + `rzp.io` URL |
| Action executor (`CREATE_PAYMENT_LINK`, `CONTACT_CUSTOMER`, `COLLECT_PROMISE_TO_PAY`) | **IMPLEMENTED / PARTIAL** | `app/services/action_executor.py:38`, `tests/test_action_executor.py` | `WAIT`/`WAIT_FOR_NATIVE_RETRY`/`ESCALATE`/`STOP` have no external side effect — state transition only; contact actions store but do not deliver messages |
| Payment recovery (`payment.captured` / `subscription.charged` → `RECOVERED`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py:190`, `tests/test_webhooks.py:132` | Matches on `RevenueCase.razorpay_payment_id`; cancelled scheduled actions (`CANCELLED`) |
| Payment Link paid (`payment_link.paid` → `RECOVERED`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py:215`, `tests/test_webhooks.py:219` | Matches on `RevenueCase.razorpay_payment_link_id` (different payment than original failure); `payment_link.paid` with unknown `link_id` is a noop |
| Dispute handling (`payment.dispute.created` + customer reply `dispute`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py:248`, `tests/test_customer_reply.py:68`, `tests/test_webhooks.py:250` | Dispute **overrides** terminal states (including `RECOVERED`); cancels all `SCHEDULED` actions |
| Promise-to-Pay extraction (regex heuristic / Anthropic JSON) | **SIMULATED / PARTIAL** (default) · **TEST_MODE** when `LLM_API_ENABLED=true` | `app/services/llm_client.py:181`, `tests/test_llm_client.py` | Default: keyword `DISPUTE_PHRASES`, regex `AMOUNT_PATTERN` + weekday/relative-date parser; live: strictly `anthropic` only (`claude-3-5-haiku-latest`), other providers rejected |
| Promise-to-Pay validation | **IMPLEMENTED** | `app/services/ptp_extractor.py:38`, `tests/test_customer_reply.py:98` | Checks intent, confidence ≥ 0.6, amount ≤ outstanding, date ∈ [today, today+14d]; invalid → `HUMAN_REVIEW` |
| Promise-to-Pay scheduling + follow-up processor | **PARTIAL / MANUAL_ONLY** | `app/services/orchestrator.py:363` (schedules `FOLLOW_UP_PTP`), `app/services/ptp_followup.py:38`, `scripts/process_followups.py` | No automatic scheduler/worker; `FOLLOW_UP_PTP` fires only when `python scripts/process_followups.py` is invoked manually or via external cron; `WAIT`/`WAIT_FOR_NATIVE_RETRY` rows are `SCHEDULED` and never woken automatically |
| Customer message drafting (templated / Anthropic) | **SIMULATED** (default) · **TEST_MODE** when enabled | `app/services/llm_client.py:91`, `app/services/action_executor.py:100` | Stores `CustomerMessage(direction=outbound)` with `channel=simulated|llm`; no SMS/email/WhatsApp transport exists |
| Customer-reply ingestion (`POST /cases/{id}/customer-reply`) | **IMPLEMENTED / VERIFIED** | `app/routers/cases.py:160`, `tests/test_customer_reply.py` | Handles `promise_to_pay` / `dispute` / `unclear`; respects `REPLYABLE_STATES`; `PROMISE_TO_PAY` → `AWAITING_OUTCOME` + scheduled `FOLLOW_UP_PTP` |
| Adaptive ML policy (HistGradientBoostingClassifier, expected-value scorer) | **IMPLEMENTED · SYNTHETIC_ONLY · NOT LIVE** | `app/ml/train.py:59`, `app/ml/scorer.py:99`, `app/services/ml_policy.py:49` | Trained on synthetic data from `ground_truth.py`; evaluated offline only; **not called by `orchestrator.handle_event`** (which calls `policy_engine.decide`) |
| Synthetic training data generator | **IMPLEMENTED / SYNTHETIC_ONLY** | `app/ml/synthetic_history.py:45` | Uniform action sampling to expose all (category, action) pairs to the model |
| Offline experiment runner (baseline vs adaptive, common-random-numbers) | **IMPLEMENTED / VERIFIED** | `app/services/experiment_runner.py:47`, `tests/test_experiments.py` | Deterministic per `(seed, model)`; up to 5,000 cases per API call |
| Experiment persistence / API / CSV export | **IMPLEMENTED / VERIFIED** | `app/routers/experiments.py:22`, `tests/test_experiments.py:91` | `ExperimentCase` rows keyed by `run_id`; `GET /experiments/{id}/export.csv` returns header + `count*2` rows |
| React merchant dashboard | **IMPLEMENTED / PARTIAL** | `frontend/src/App.tsx`, `frontend/src/ExperimentPanel.tsx` | Lists cases + health + experiment runner; `GET /cases/{id}` exists but has **no rendered detail view** |
| PostgreSQL as authoritative state | **IMPLEMENTED / VERIFIED** | `backend/alembic/versions/*`, `app/models.py`, `docker-compose.yml:7` | 9 tables; `payment_events`/`decisions`/`audit_events` append-only; see Data Model in `ARCHITECTURE.md` |
| Redis/RQ delayed-action runtime | **PLANNED — dependency reserved only** | `docker-compose.yml:19`, `backend/requirements.txt:10`, `app/core/config.py:19` | `redis`/`rq` installed but **no import** in `backend/app/**` or `backend/scripts/**` besides a comment in `ptp_followup.py:6`; Compose service exists for future work |
| Real inbound Razorpay webhooks (production live-money) | **NOT IMPLEMENTED** — `TEST_MODE_ONLY` verified, production explicitly rejected | `app/services/razorpay_client.py:72` rejects non-`rzp_test_*` keys | Webhook verification is provider-shape-agnostic, but the only live API path validated is Test Mode Payment Links |
| Deployment (public URL, prod DB, secrets rotation) | **PLANNED** | — | No Dockerfile for app, no CI deploy job, no auth on API |

---

## Runtime reality — what `SCHEDULED` means today

| Action type | Created as | Transitions to `EXECUTED` when… | Case state while `SCHEDULED` | Automatic wake-up? |
|-------------|------------|----------------------------------|------------------------------|--------------------|
| `WAIT` | `SCHEDULED` by `policy_engine.py:155`, state → `WAITING` | **Never automatically** | `WAITING` | **No** — persists as `SCHEDULED` forever |
| `WAIT_FOR_NATIVE_RETRY` | `SCHEDULED`, state → `WAITING` | **Never automatically** | `WAITING` | **No** |
| `CREATE_PAYMENT_LINK` | `SCHEDULED` → immediately `EXECUTED` by `action_executor.py:56` | Synchronously in the webhook request | `AWAITING_OUTCOME` | N/A — executed inline |
| `CONTACT_CUSTOMER` / `COLLECT_PROMISE_TO_PAY` | `SCHEDULED` → immediately `EXECUTED` | Synchronously in the webhook request; drafts stored as `CustomerMessage` | `AWAITING_OUTCOME` | N/A — executed inline |
| `FOLLOW_UP_PTP` | `SCHEDULED` by `orchestrator.py:363` with `scheduled_for = promised_date` | Only when `process_due_followups(db)` is called manually (`scripts/process_followups.py`) and `scheduled_for <= now` | `AWAITING_OUTCOME` | **No — MANUAL_ONLY** |
| `ESCALATE` | `SCHEDULED`, state → `HUMAN_REVIEW` | Terminal automation state — no execution step | `HUMAN_REVIEW` | No |
| `STOP` | `SCHEDULED`, state → `STOPPED` | Terminal | `STOPPED` | No |

**Implication:** `WAITING` and `AWAITING_OUTCOME` are currently "parked" states. No clock drives a transition out of them except: (a) an inbound `payment.captured` / `payment_link.paid` / `payment.dispute.created` webhook, (b) a `POST /cases/{id}/customer-reply`, or (c) an operator running `process_followups.py`. The next planned phase (Temporal Recovery Runtime) is intended to make `WAITING`/`FOLLOW_UP_PTP` automatic — but **that runtime does not exist yet**.

---

## Known technical debt

1. **No automatic delayed-action runtime** — `Redis`/`RQ` are dependencies with no imports; `WAIT` and `FOLLOW_UP_PTP` require manual processing. Recovery depends entirely on future inbound webhooks or operator action once a case is parked.
2. **`DECISION_READY` is vestigial in reply handling** — `REPLYABLE_STATES` includes `DECISION_READY` (`orchestrator.py:294`) but `policy_engine.decide` transitions through `DECISION_READY` synchronously within the same request, so the state is never persisted long enough to receive a reply. Harmless but misleading.
3. **Duplicate case lookup is naive** — `_handle_failure` finds an existing `RevenueCase` only by exact `razorpay_payment_id` match. Near-duplicates with differing ID formatting would create separate cases.
4. **Customer identity is synthetic** — `Customer` rows are created but never enriched from Razorpay customer/subscription entities; `external_ref` is rarely set.
5. **Contact fatigue is per-case only** — `_contacts_for_case` counts `CONTACT_CUSTOMER`/`CREATE_PAYMENT_LINK`/`COLLECT_PROMISE_TO_PAY` actions for a single `RevenueCase`, not per-customer or per-merchant rolling windows.
6. **Time-zone handling is naive** — `app/core/time.py:5` returns `datetime` without `tzinfo` to match the existing `DateTime` schema; all code assumes UTC.
7. **Model artifact is gitignored** — `backend/app/ml/artifacts/` is in `.gitignore:8`; a fresh clone has no `model.joblib` until `python -m app.ml.train` is run. CI trains from scratch each run.
8. **Payload shape fragility** — `_entity_from_payload` loops over `payload["payload"].values()` to find an `entity` key. Accepts both `payload.payment.entity` and `payload.subscription.entity` but would silently pick the wrong entity if a webhook nests multiple object types.
9. **No API authentication** — every route (`/cases`, `/webhooks/razorpay`, `/experiments`) is unauthenticated; webhook authenticity relies solely on `x-razorpay-signature` + `x-razorpay-event-id`.
10. **Frontend has no case-detail renderer** — `GET /cases/{id}` returns `decisions`, `actions`, `messages`, `promises_to_pay`, and `audit_trail`, but `App.tsx` never renders it (confirmed in `frontend/src/App.tsx:113` — only the list table).

---

## Next planned subsystem

> **Temporal Recovery Runtime / State Correctness — PLANNED, not implemented.**
>
> Intended scope (subject to `docs/DECISIONS.md` and `ARCHITECTURE.md`): a durable wake-up mechanism for parked states (`WAITING` → re-evaluate, `AWAITING_OUTCOME` with due `FOLLOW_UP_PTP` → `HUMAN_REVIEW`/`RECOVERED`), treating PostgreSQL as the source of truth and Redis only as transport, plus state-correctness invariants for terminal transitions. No design doc for this phase has been committed yet. Any claim that this runtime exists is incorrect.

---

## Documentation status language

| Label | Meaning |
|-------|---------|
| **IMPLEMENTED** | Code exists and is on the live runtime path. |
| **VERIFIED** | Independent evidence (automated test or manual probe) confirms the behavior. |
| **PARTIAL** | Some implementation exists but a required runtime behavior is missing. |
| **SIMULATED** | Local deterministic stand-in — no external provider call. |
| **TEST_MODE_ONLY** | Real provider call, but only against the provider's test/sandbox environment. |
| **SYNTHETIC_ONLY** | Uses generated or simulated data/outcomes, not real Razorpay traffic. |
| **MANUAL_ONLY** | Requires an explicit operator script/cron invocation; does not fire automatically. |
| **PLANNED** | Not currently implemented; design may exist but no code path. |
| **DEPRECATED / STALE** | Retained for historical context only; should not be relied on. |

---

## Anti-drift rule

**Documentation is part of the implementation.** A future subsystem is not considered complete until its canonical documentation still describing the previous behavior has been updated. Every implementation report must list:

- documentation files that **changed**,
- documentation files **intentionally unchanged**,
- and the **reason**.

Avoid repeating volatile numbers (test count, metrics) in more than one document — the canonical home for the current verified test count is this file.

