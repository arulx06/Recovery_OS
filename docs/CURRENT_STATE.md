# RecoveryOS — Current State

**Canonical living document for "what exists RIGHT NOW." Future implementation phases must update this file before any other documentation.**

> Status legend — see [Documentation Status Language](#documentation-status-language).

---

## Repository baseline

| Property | Value |
|----------|-------|
| Branch | `feat/payment-link-reconciliation` |
| Baseline ancestry | Verified hardening baseline (`chore: harden RecoveryOS baseline`, `chore: establish verified RecoveryOS baseline`) + temporal runtime `3debc27` |
| Backend tests | 292 passed (hermetic, `sqlite://` temp DB, external network and Redis denied) |
| Frontend `npm run build` | PASS |
| Frontend `npm run lint` (`oxlint`) | PASS |
| Database | PostgreSQL (required); SQLite for tests/CI |
| Redis/RQ | Implemented as reconstructable action transport; PostgreSQL remains authoritative |
| Model artifact | `backend/app/ml/artifacts/model.joblib` — gitignored, reproduced via `python -m app.ml.train` |

> Do not hardcode a commit hash here. The branch name is the stable reference. If a specific hash must be cited for a report, add it locally and do not commit it.

### How this branch was verified

- `python -m pytest -q` -> 292 passed with socket-denial guard, fake credentials, and `TASK_QUEUE_ENABLED=false` (`conftest.py`).
- `npm run build` and `npm run lint` pass on Node 22 / Vite 8.
- `alembic upgrade head` applies cleanly on SQLite (`ci.yml`) and on local PostgreSQL 16 at revision `8d6e24f91a73` (no new migration — existing Action fields suffice for reconciliation).
- A real Redis/RQ smoke test verified an ID-only `WAIT` job through `app.worker.WindowsWorker`; the default RQ worker is not Windows-compatible.
- Manual Razorpay TEST MODE path verified independently: `payment.failed` (`card_expired`) → `INVALID_INSTRUMENT` → `CREATE_PAYMENT_LINK` → `plink_*` via `https://api.razorpay.com/v1/payment_links` → `rzp.io` short URL, with `Action.result.simulated == false` and `reference_id == Action.id` when `RAZORPAY_API_ENABLED=true` and `rzp_test_*` credentials are present. Provider reconciliation via `list_payment_links?reference_id=<Action.id>` validated against official docs and mocked tests — genuine `plink_*` found without a second create.

---

## Capability matrix

| Capability | Status | Evidence | Limitations |
|------------|--------|----------|-------------|
| Webhook signature verification (HMAC-SHA256 on raw body) | **IMPLEMENTED / VERIFIED** | `app/core/security.py:11`, `app/routers/webhooks.py:39`, `tests/test_webhooks.py:35` | Fails closed when `RAZORPAY_WEBHOOK_SECRET` empty; tests cover malformed JSON, non-object body, missing `x-razorpay-event-id` |
| Idempotent ingestion (`payment_events.razorpay_event_id` unique) | **IMPLEMENTED / VERIFIED** | `app/routers/webhooks.py`, migrations, `tests/test_webhooks.py` | Stores normalized provider IDs; an out-of-order recovery signal prevents later failure automation |
| Failure diagnosis (deterministic taxonomy, 8 categories) | **IMPLEMENTED / VERIFIED** | `app/services/failure_diagnosis.py:43`, `tests/test_failure_diagnosis.py` | 8 fixed categories; anything unmatched → `UNKNOWN` → conservative `ESCALATE`; see `docs/SYSTEM_FLOWS.md` |
| Case state machine (10 states, guarded transitions) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py`, `app/services/temporal_runtime.py`, `tests/test_temporal_runtime.py` | `DECISION_READY` is ephemeral; dispute can override `RECOVERED` (by design) |
| Baseline guardrail + policy engine | **IMPLEMENTED / VERIFIED** | `app/services/policy_engine.py:176`, `tests/test_policy_engine.py` | `max_contacts_per_case=3`, `max_contacts_per_7_days=2`, `min_contact_interval_hours=12`, `max_automated_amount=₹25,000`, `max_total_attempts=5`; contact limits use `Action` history |
| Deterministic guardrails around adaptive policy | **IMPLEMENTED** | `app/services/ml_policy.py:71` reuses `policy_engine.check_action_allowed` + `record_decision` | Adaptive scorer never bypasses guardrails |
| Payment Link creation — simulated | **IMPLEMENTED / VERIFIED** | `app/services/razorpay_client.py:65`, `tests/test_razorpay_client.py:35`, `tests/test_webhooks.py:176` | Default; `id=plink_sim_*`, `short_url=https://rzp.io/simulated/*`, `simulated=true` |
| Payment Link creation — Razorpay Test Mode live call | **TEST_MODE_ONLY / VERIFIED** | `app/services/razorpay_client.py:48` (`httpx.post` to `https://api.razorpay.com/v1/payment_links`, `reference_id=Action.id`, `notes={recoveryos_case_id, recoveryos_action_id}`), `tests/test_razorpay_client.py:53` | Gated by `RAZORPAY_API_ENABLED=true` **and** `rzp_test_*` key; live-mode keys rejected; verified manually to return genuine `plink_*` + `rzp.io` URL; `reference_id` is stable Action-scoped identity |
| Payment Link provider reconciliation | **IMPLEMENTED / VERIFIED** | `app/services/razorpay_client.py:184` (`list_payment_links_by_reference`), `app/services/temporal_runtime.py:234` (`_handle_ambiguous_*`, `_reconcile_stale_*`), `tests/test_payment_link_reconciliation.py:100`, `scripts/reconcile_payment_links.py` | Finds existing provider link by `reference_id=Action.id`; validates `amount`/`currency`/`notes`/`status` before adoption; `expired`/`cancelled` → `HUMAN_REVIEW`; lookup itself is `RazorpayAmbiguousError`-aware; bounded by `max_attempts` |
| Ambiguous provider outcome handling | **IMPLEMENTED / VERIFIED** | `app/services/razorpay_client.py:27` (`RazorpayAmbiguousError`), `app/services/temporal_runtime.py:235`, `tests/test_payment_link_reconciliation.py:136` | `Timeout`/`NetworkError`/`5xx`/`429`/`duplicate reference_id` → `RazorpayAmbiguousError` → reconcile before retry; definite `400` validation → `RazorpayAPIError` → bounded retry then `HUMAN_REVIEW`; never blindly creates another link |
| Action executor (`CREATE_PAYMENT_LINK`, `CONTACT_CUSTOMER`, `COLLECT_PROMISE_TO_PAY`) | **IMPLEMENTED / VERIFIED** | `app/services/action_executor.py`, `app/services/temporal_runtime.py`, `tests/test_temporal_runtime.py` | Worker commits its claim before provider calls; provider HTTP is outside any DB transaction; contact actions store but do not deliver messages; payment-link success/failure is sanitized (`_sanitize_link`) and audited as `action_executed` vs `action_reconciled` |
| Payment recovery (`payment.captured` / `subscription.charged` -> `RECOVERED`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py`, `tests/test_temporal_runtime.py` | Correlates payment or subscription IDs; also recovers `STOPPED`; cancels scheduled actions and marks pending promises `KEPT` |
| Payment Link paid (`payment_link.paid` → `RECOVERED`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py:215`, `tests/test_webhooks.py:219` | Matches on `RevenueCase.razorpay_payment_link_id` (different payment than original failure); `payment_link.paid` with unknown `link_id` is a noop |
| Dispute handling (`payment.dispute.created` + customer reply `dispute`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py:248`, `tests/test_customer_reply.py:68`, `tests/test_webhooks.py:250` | Dispute **overrides** terminal states (including `RECOVERED`); cancels all `SCHEDULED` actions |
| Promise-to-Pay extraction (regex heuristic / Anthropic JSON) | **SIMULATED / PARTIAL** (default) · **TEST_MODE** when `LLM_API_ENABLED=true` | `app/services/llm_client.py:181`, `tests/test_llm_client.py` | Default: keyword `DISPUTE_PHRASES`, regex `AMOUNT_PATTERN` + weekday/relative-date parser; live: strictly `anthropic` only (`claude-3-5-haiku-latest`), other providers rejected |
| Promise-to-Pay validation | **IMPLEMENTED** | `app/services/ptp_extractor.py:38`, `tests/test_customer_reply.py:98` | Checks intent, confidence ≥ 0.6, amount ≤ outstanding, date ∈ [today, today+14d]; invalid → `HUMAN_REVIEW` |
| Promise-to-Pay scheduling + follow-up processor | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py`, `app/services/ptp_followup.py`, `tests/test_temporal_runtime.py` | Follow-up is linked to one promise and scheduled after the configured merchant-local promise day; no messaging transport exists |
| Customer message drafting (templated / Anthropic) | **SIMULATED** (default) · **TEST_MODE** when enabled | `app/services/llm_client.py:91`, `app/services/action_executor.py:100` | Stores `CustomerMessage(direction=outbound)` with `channel=simulated|llm`; no SMS/email/WhatsApp transport exists |
| Customer-reply ingestion (`POST /cases/{id}/customer-reply`) | **IMPLEMENTED / VERIFIED** | `app/routers/cases.py:160`, `tests/test_customer_reply.py` | Handles `promise_to_pay` / `dispute` / `unclear`; respects `REPLYABLE_STATES`; `PROMISE_TO_PAY` → `AWAITING_OUTCOME` + scheduled `FOLLOW_UP_PTP` |
| Adaptive ML policy (HistGradientBoostingClassifier, expected-value scorer) | **IMPLEMENTED · SYNTHETIC_ONLY · NOT LIVE** | `app/ml/train.py:59`, `app/ml/scorer.py:99`, `app/services/ml_policy.py:49` | Trained on synthetic data from `ground_truth.py`; evaluated offline only; **not called by `orchestrator.handle_event`** (which calls `policy_engine.decide`) |
| Synthetic training data generator | **IMPLEMENTED / SYNTHETIC_ONLY** | `app/ml/synthetic_history.py:45` | Uniform action sampling to expose all (category, action) pairs to the model |
| Offline experiment runner (baseline vs adaptive, common-random-numbers) | **IMPLEMENTED / VERIFIED** | `app/services/experiment_runner.py:47`, `tests/test_experiments.py` | Deterministic per `(seed, model)`; up to 5,000 cases per API call |
| Experiment persistence / API / CSV export | **IMPLEMENTED / VERIFIED** | `app/routers/experiments.py:22`, `tests/test_experiments.py:91` | `ExperimentCase` rows keyed by `run_id`; `GET /experiments/{id}/export.csv` returns header + `count*2` rows |
| React merchant dashboard | **IMPLEMENTED / PARTIAL** | `frontend/src/App.tsx`, `frontend/src/ExperimentPanel.tsx` | Lists cases + health + experiment runner; `GET /cases/{id}` exists but has **no rendered detail view** |
| PostgreSQL as authoritative state | **IMPLEMENTED / VERIFIED** | migration `8d6e24f91a73`, `app/models.py`, `app/services/temporal_runtime.py` | `Action` owns schedule, status, claim lease, attempts, and queue metadata; Redis loss is repaired by reconciliation |
| Redis/RQ delayed-action runtime | **IMPLEMENTED / VERIFIED** | `app/services/task_queue.py`, `app/jobs.py`, `app/worker.py`, `scripts/reconcile_actions.py` | Worker process and `--with-scheduler` must be running; Windows requires `app.worker.WindowsWorker` |
| Real inbound Razorpay webhooks (production live-money) | **NOT IMPLEMENTED** — `TEST_MODE_ONLY` verified, production explicitly rejected | `app/services/razorpay_client.py:72` rejects non-`rzp_test_*` keys | Webhook verification is provider-shape-agnostic, but the only live API path validated is Test Mode Payment Links |
| Deployment (public URL, prod DB, secrets rotation) | **PLANNED** | — | No Dockerfile for app, no CI deploy job, no auth on API |

---

## Runtime reality - what `SCHEDULED` means today

| Action type | Created as | Transitions to `EXECUTED` when… | Case state while `SCHEDULED` | Automatic wake-up? |
|-------------|------------|----------------------------------|------------------------------|--------------------|
| `WAIT` | `SCHEDULED` for `now + WAIT_DELAY_SECONDS`, case -> `WAITING` | Worker atomically claims it, marks it complete, and re-runs baseline policy | `WAITING` | Yes, via RQ scheduler |
| `WAIT_FOR_NATIVE_RETRY` | `SCHEDULED` for `now + NATIVE_RETRY_DELAY_SECONDS` | Same durable wake-up path as `WAIT` | `WAITING` | Yes |
| `CREATE_PAYMENT_LINK` | `SCHEDULED` after webhook commit | Worker claims `EXECUTING`, then: ambiguous outcome → provider lookup `list?reference_id=Action.id`→ validated adopt via `action_reconciled` **without second create**; reliably absent → bounded retry; mismatch/expired → `HUMAN_REVIEW`. All provider HTTP is outside any DB transaction. | `ACTION_SCHEDULED` | Yes (RQ + provider reconciliation) |
| `CONTACT_CUSTOMER` / `COLLECT_PROMISE_TO_PAY` | `SCHEDULED` after webhook commit | Worker drafts outside a DB transaction, then stores the message | `ACTION_SCHEDULED` | Yes |
| `FOLLOW_UP_PTP` | `SCHEDULED` at exclusive end of configured merchant-local promise date | Worker resolves the exact linked promise to `BROKEN`, or no-ops if stale | `AWAITING_OUTCOME` | Yes |
| `ESCALATE` | `EXECUTED`, case -> `HUMAN_REVIEW` | Recorded immediately; no transport operation | `HUMAN_REVIEW` | Not applicable |
| `STOP` | `EXECUTED`, case -> `STOPPED` | Recorded immediately; no transport operation | `STOPPED` | Not applicable |

The request transaction commits `PaymentEvent`, case, decision, and action before `enqueue_case_actions` touches Redis. A publish failure is audited and leaves the action `SCHEDULED`; `python scripts/reconcile_actions.py` republishes it. Workers atomically claim `SCHEDULED -> EXECUTING`, use a bounded attempt budget, and treat duplicate/stale jobs as no-ops.

---

## Known technical debt

1. **Worker/reconciliation processes are operational dependencies** - committed actions remain safe if they stop, but execution is delayed until a worker and periodic reconciliation resume.
2. **Provider exactly-once remains bounded by provider primitive** - Razorpay enforces uniqueness on `reference_id` (400 on duplicate) and supports `list?reference_id=` filtering, which this stage uses for reconciliation. A provider crash between `POST /payment_links` commit on Razorpay's side and response transmission still requires one extra `GET /payment_links?reference_id=` to discover the already-created link. DB claim + `reference_id` + reconciliation reduce ambiguity to a single validated lookup, but true exactly-once still depends on Razorpay's documented uniqueness guarantee, not on DB idempotency alone.
3. **`DECISION_READY` is vestigial in reply handling** - it is an ephemeral policy state.
4. **Customer identity is synthetic** - `Customer` rows are not enriched from Razorpay customer/subscription entities.
5. **Contact fatigue is per-case only** - limits are not aggregated per customer or merchant.
6. **Database timestamps remain naive UTC** - merchant-local conversion is explicit only for PTP business dates.
7. **Model artifact is gitignored** - a fresh clone must run `python -m app.ml.train` for experiments.
8. **No API authentication** - webhook authenticity relies on signature and event ID; other routes are unauthenticated.
9. **Frontend has no case-detail renderer** - the API exposes temporal metadata (`provider_ambiguous` / `reconciled` / `reference_id`) but the dashboard does not render the new `action_reconciled` / `payment_link_reconciliation_*` audit trail distinctly.

---

## Next planned subsystem

> **LIVE ADAPTIVE POLICY + FRICTION-AWARE DECISIONING — PLANNED, not implemented.**
>
> With provider-side reconciliation complete, the next major stage wires the offline adaptive ML scorer into real runtime decisions behind a feature flag, retains deterministic baseline fallback, exposes model provenance, redesigns expected utility to penalize customer friction, and improves evaluation methodology. See `docs/DECISIONS.md` and `docs/ML_AND_EVALUATION.md` for the retained limitation (`adaptive synthetic recovery improves but contacts increase substantially`) that this next stage will address.

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
