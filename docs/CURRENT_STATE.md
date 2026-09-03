# RecoveryOS — Current State

**Canonical living document for "what exists RIGHT NOW." Future implementation phases must update this file before any other documentation.**

> Status legend — see [Documentation Status Language](#documentation-status-language).

---

## Repository baseline

| Property | Value |
|----------|-------|
| Branch | `feat/release-hardening-e2e` |
| Baseline ancestry | Verified baseline `3debc27` + reconciliation `1a980d6` + adaptive `a624341` + LLM intelligence `4e64c1c` + observability `d5474c5` |
| Backend tests | 471 passed (hermetic, `sqlite://` temp DB, external network and Redis denied) — includes 37 release-hardening tests |
| Frontend `npm run build` | PASS (24 modules, 268kB) |
| Frontend `npm run lint` (`oxlint`) | PASS (warnings only) |
| Database | PostgreSQL (required); SQLite for tests/CI — head `c9d0e1f2a3b4` (LLM provenance) — hardening adds no migration (middleware, auth, logging, readiness are code-only) |
| Redis/RQ | Implemented as reconstructable action transport; PostgreSQL remains authoritative — hardening adds failure-injection guard + stale EXECUTING reconciliation already verified |
| Model artifact | `backend/app/ml/artifacts/model.joblib` + `manifest.json` — gitignored for source runs; `backend/.dockerignore` also excludes host artifacts, then `backend/Dockerfile` trains and validates a fresh artifact during image build |

> Do not hardcode a commit hash here. The branch name is the stable reference. If a specific hash must be cited for a report, add it locally and do not commit it.

### How this branch was verified

- `python -m pytest -q` -> 471 passed (hermetic, `sqlite://` temp DB, external network and Redis denied, 37 release-hardening tests); hardening includes sensitive read auth, public health/webhook exemptions, CORS wildcard validation, demo safety, resilience, and sanitization.
- `npm run build` and `npm run lint` pass on Node 22 / Vite 8 (console build: 24 modules, ~268kB, with polling and sessionStorage operator token).
- `alembic upgrade head` clean at `c9d0e1f2a3b4` — hardening adds **no migration** (middleware, auth, logging, readiness are code-only; `validate_startup_config` is stateless).
- A real Redis/RQ smoke test verified an ID-only `WAIT` job through `app.worker.WindowsWorker`; the default RQ worker is not Windows-compatible.
- Razorpay TEST MODE path is implemented and hermetically tested (`payment.failed` → `CREATE_PAYMENT_LINK`, request shape, `reference_id=Action.id`, and reconciliation lookup). A real provider smoke was **NOT PERFORMED** during the current corrective pass; use `docs/PUBLIC_WEBHOOK.md` only with `rzp_test_*` credentials.
- Adaptive policy verified: `RECOVERY_POLICY=baseline` (default), `shadow` (baseline executes + `shadow_adaptive_recommendation` audit, no second Action), `adaptive` (friction-aware `P*amount - cost - weight*friction`, provenance, fallback to baseline on model failure). The `recovery-v1` manifest records Brier 0.1597 and the SHA-256 of the generated artifact; the fingerprint is build-specific rather than a hard-coded release identifier.
- LLM-assisted intelligence verified: `LLM_API_ENABLED=false` (default) → deterministic templates + regex extraction; `LLM_API_ENABLED=true` with mocked Anthropic → structured PTP extraction `{intent, promised_amount, promised_date, confidence, reasoning_code}` + draft with `[[PAYMENT_LINK]]` placeholder substituted deterministically with authoritative `short_url`; prompt versions `ptp-v1`/`message-v1` and provenance stored; provider failure or malformed response → deterministic fallback; injection downgraded; `/health` and `/ready` sanitized (no secrets).
- Observability verified: `GET /dashboard/summary` computes revenue at risk/recovered, state/case counts, PTP, policy/model, queue, Razorpay, LLM without inventing values; `GET /cases` supports state/category/latest-action/latest-policy/search + pagination; `GET /cases/{id}` enriches with failure explanation, persisted decision-time inspectors, explicitly current guardrail/friction surfaces, chronological timeline, and exact-Action provider reconciliation truth; `scripts/seed_demo.py` always creates five core rows and adds genuine adaptive/shadow D/F rows only when a trained model is available; frontend passes build with Revenue-vs-friction Pareto, polling (10s case detail, 30s dashboard), and operator token (sessionStorage); no secret/CoT exposure.
- Hardening verified: `/health` liveness vs `/ready` readiness, optional `DEMO_ADMIN_TOKEN_ENABLED` protects sensitive reads and mutations while `/webhooks/razorpay` remains HMAC-only, request correlation, structured logging, startup validation, bounded experiments, unambiguous CORS, failure injection, queue orphan recovery, Docker context secret exclusion, and offline SIMULATED fallback.

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
| Payment Link creation — Razorpay Test Mode live call | **TEST_MODE_ONLY / MANUAL SMOKE PENDING** | `app/services/razorpay_client.py` and hermetic `tests/test_razorpay_client.py` | Gated by `RAZORPAY_API_ENABLED=true` and `rzp_test_*`; live keys rejected; request/response/reconciliation behavior tested without network; no current-pass provider call |
| Payment Link provider reconciliation | **IMPLEMENTED / VERIFIED** | `app/services/razorpay_client.py:184` (`list_payment_links_by_reference`), `app/services/temporal_runtime.py:234` (`_handle_ambiguous_*`, `_reconcile_stale_*`), `tests/test_payment_link_reconciliation.py:100`, `scripts/reconcile_payment_links.py` | Finds existing provider link by `reference_id=Action.id`; validates `amount`/`currency`/`notes`/`status` before adoption; `expired`/`cancelled` → `HUMAN_REVIEW`; lookup itself is `RazorpayAmbiguousError`-aware; bounded by `max_attempts` |
| Ambiguous provider outcome handling | **IMPLEMENTED / VERIFIED** | `app/services/razorpay_client.py:27` (`RazorpayAmbiguousError`), `app/services/temporal_runtime.py:235`, `tests/test_payment_link_reconciliation.py:136` | `Timeout`/`NetworkError`/`5xx`/`429`/`duplicate reference_id` → `RazorpayAmbiguousError` → reconcile before retry; definite `400` validation → `RazorpayAPIError` → bounded retry then `HUMAN_REVIEW`; never blindly creates another link |
| Action executor (`CREATE_PAYMENT_LINK`, `CONTACT_CUSTOMER`, `COLLECT_PROMISE_TO_PAY`) | **IMPLEMENTED / VERIFIED** | `app/services/action_executor.py`, `app/services/temporal_runtime.py`, `tests/test_temporal_runtime.py` | Worker commits its claim before provider calls; provider HTTP is outside any DB transaction; contact actions store but do not deliver messages; payment-link success/failure is sanitized (`_sanitize_link`) and audited as `action_executed` vs `action_reconciled` |
| Payment recovery (`payment.captured` / `subscription.charged` -> `RECOVERED`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py`, `tests/test_temporal_runtime.py` | Correlates payment or subscription IDs; also recovers `STOPPED`; cancels scheduled actions and marks pending promises `KEPT` |
| Payment Link paid (`payment_link.paid` → `RECOVERED`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py:215`, `tests/test_webhooks.py:219` | Matches on `RevenueCase.razorpay_payment_link_id` (different payment than original failure); `payment_link.paid` with unknown `link_id` is a noop |
| Dispute handling (`payment.dispute.created` + customer reply `dispute`) | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py:248`, `tests/test_customer_reply.py:68`, `tests/test_webhooks.py:250` | Dispute **overrides** terminal states (including `RECOVERED`); cancels all `SCHEDULED` actions |
| LLM PTP extraction (structured + deterministic validation) | **IMPLEMENTED / OPTIONAL** ( `LLM_API_ENABLED=false` deterministic, `LLM_API_ENABLED=true` + `LLM_PTP_EXTRACTION_ENABLED` → Anthropic structured) | `app/services/llm_client.py:470` (`PTPExtraction` `{intent, promised_amount, promised_date, confidence, reasoning_code}` `ptp-v1`/`ptp-schema-v1`), `app/services/ptp_extractor.py:38`, `tests/test_llm_stage.py`, `tests/test_customer_reply.py` | Deterministic fallback always available; structured output validated (enum, amount >0, date YYYY-MM-DD, confidence 0..1); direct malformed output raises `LLMInvalidResponseError`, while the wrapper records deterministic fallback provenance; ambiguous or injection-marked text → `uncertain`/`HUMAN_REVIEW`; LLM never invents amount — omitted stays `HUMAN_REVIEW` with `amount_method=customer_explicit` provenance |
| LLM message drafting (safe, DRAFT only) | **IMPLEMENTED / OPTIONAL** ( `LLM_API_ENABLED=false` templated, `LLM_API_ENABLED=true` + `LLM_MESSAGE_DRAFT_ENABLED` → Anthropic with `[[PAYMENT_LINK]]` placeholder) | `app/services/llm_client.py:88` (`MESSAGE_DRAFT_PROMPT_VERSION=message-v1`, `draft_with_fallback`), `app/services/action_executor.py:43`, `tests/test_llm_stage.py` | Stores `CustomerMessage(status=DRAFT, generation_method, provider, model, prompt_version)` — never `SENT/DELIVERED`; authoritative `short_url` substituted deterministically; no invented amount/discount/fee; no internal IDs in prompt |
| Deterministic fallback | **IMPLEMENTED / VERIFIED** | `app/services/llm_client.py:658` (`draft_with_fallback`/`extract_with_fallback`, `LLMUnavailableError`/`LLMInvalidResponseError` → template/uncertain) + `app/routers/cases.py:190` (extraction outside DB tx) | LLM disabled/unavailable/timeout/malformed → deterministic template or `uncertain`/`HUMAN_REVIEW`; no HTTP 500 caused solely by LLM; recovery workflow continues |
| Promise-to-Pay validation | **IMPLEMENTED / VERIFIED** | `app/services/ptp_extractor.py:38` (`DEFAULT_HORIZON_DAYS=14`, `MAX_HORIZON=90`), `tests/test_ptp_extractor.py`, `tests/test_llm_stage.py` | Checks intent=`promise_to_pay`, confidence ≥0.6, amount positive ≤ outstanding (`customer_explicit`), date parseable, not past, ≤ horizon, not absurdly far, case eligible (not RECOVERED/DISPUTED/STOPPED); invalid → `HUMAN_REVIEW` with `ptp_validation_failed` audit |
| Promise-to-Pay provenance | **IMPLEMENTED / VERIFIED** | `app/models.py:PromiseToPay` (`extraction_method, llm_provider, llm_model, prompt_version, schema_version, amount_method, reasoning_code, source_message_id`), `a1b2c3d4e5f6`→`c9d0e1f2a3b4`, `tests/test_llm_stage.py:560` | Full provenance: `deterministic` vs `llm`/`llm_fallback_template`, provider/model/prompt/schema, confidence, amount_method, source message linkage; `HUMAN_REVIEW` fallback clearly not `llm` |
| Customer-message provenance & DRAFT status | **IMPLEMENTED / VERIFIED** | `app/models.py:CustomerMessage` (`generation_method, llm_provider, llm_model, prompt_version, schema_version, status=DRAFT`), `app/services/action_executor.py:107`, `tests/test_action_executor.py`, `tests/test_llm_stage.py` | Outbound stored as `DRAFT` (never `SENT`/`DELIVERED`); provenance distinguishes `deterministic`/`llm`/`llm_fallback_template`; `channel=simulated|llm` preserved; idempotent (no duplicate on retry) |
| Payment-Link URL safety (placeholder) | **IMPLEMENTED / VERIFIED** | `app/services/llm_client.py:47` (`[[PAYMENT_LINK]]`), `app/services/action_executor.py:43` (authoritative `short_url` substitution) | Every model-provided HTTP(S) URL is stripped; the app inserts the exact Razorpay `short_url` once, including when the model returns a placeholder plus an extra URL or multiple hallucinated URLs |
| Promise-to-Pay scheduling + follow-up processor | **IMPLEMENTED / VERIFIED** | `app/services/orchestrator.py`, `app/services/ptp_followup.py`, `tests/test_temporal_runtime.py` | Follow-up is linked to one promise and scheduled after the configured merchant-local promise day (exclusive end-of-day UTC); no messaging transport exists |
| Customer-reply ingestion (`POST /cases/{id}/customer-reply`) | **IMPLEMENTED / VERIFIED** | `app/routers/cases.py:174` (extraction outside DB tx, `extract_with_fallback`, DB errors propagate), `tests/test_customer_reply.py`, `tests/test_llm_stage.py` | Handles `promise_to_pay` / `not_a_promise` / `uncertain` / `payment_claim` / `dispute` / `unclear`; respects `REPLYABLE_STATES`; `HUMAN_REVIEW` on ambiguous/injection/opt-out; `DISPUTED` for payment_claim; no `RECOVERED` from customer text; worker fallback ensures no 500 |
| LLM failure isolation | **IMPLEMENTED / VERIFIED** | `app/services/llm_client.py:48` (`LLMUnavailableError`/`LLMInvalidResponseError`/`LLMTimeoutError`), `app/services/action_executor.py:43` (`EXPECTED_EXECUTION_ERRORS` + fallback), `app/routers/cases.py:190` | LLM errors → deterministic fallback, never swallow DB errors; controller (`WAIT/CONTACT/LINK/PTP/ESCALATE/STOP`) unchanged by LLM; guardrails not bypassed; friction/policy unchanged |
| Payment truth | **PROVIDER_AUTHORITATIVE** | `app/services/orchestrator.py:298` (`_recover_case` only via `payment.captured`/`subscription.charged`/`payment_link.paid`), `tests/test_webhooks.py` | Customer text `payment_claim`/`already paid` → `DISPUTED`/`HUMAN_REVIEW`, never `RECOVERED`; only Razorpay webhook establishes recovery |
| Customer delivery | **NOT IMPLEMENTED / MANUAL_ONLY** | — | `CustomerMessage` is `DRAFT` / stored only; no Twilio/SendGrid/WhatsApp; dashboard shows `DRAFT / NOT SENT` |
| LLM action selection | **NOT ALLOWED** | `app/services/llm_client.py` + `app/services/policy_dispatcher.py` | LLM never chooses financial/recovery actions; deterministic/guardrails + guarded adaptive remain sole controllers; tests prove isolation |
| LLM payment-state change | **NOT ALLOWED** | `app/services/orchestrator.py` | LLM cannot set `RECOVERED`/`DISPUTED` directly beyond conservative `DISPUTED` classification; `STOP`/`RECOVERED` only via orchestrator/webhook truth |
| Live adaptive policy (HistGradientBoostingClassifier, friction-aware utility) | **IMPLEMENTED / VERIFIED** | `app/ml/train.py` + `manifest.json`, `app/ml/scorer.py` (manifest + fingerprint), `app/ml/features.py` (canonical), `app/ml/friction.py`, `app/services/ml_policy.py`, `app/services/policy_dispatcher.py`, `tests/test_adaptive_policy.py` | `RECOVERY_POLICY=baseline` (default), `shadow` (baseline+audited recommendation), `adaptive` (balanced profile `weight 18`); model `recovery-v1`, build-specific SHA-256, Brier 0.1597; synthetic-only training/evaluation retained |
| Policy dispatcher + safe fallback | **IMPLEMENTED / VERIFIED** | `app/services/policy_dispatcher.py`, `app/services/ml_policy.py:184`, `tests/test_adaptive_policy.py` | Stopping rule + guardrails remain authoritative; adaptive failure → `adaptive_fallback` audit + baseline `Decision` (never stranded `DIAGNOSED`); incompatible manifest → fallback |
| Model provenance & fingerprint | **IMPLEMENTED / VERIFIED** | `app/ml/manifest.py`, `app/ml/train.py`, `app/ml/scorer.py:get_model_info`, `app/models.py:Decision` provenance columns, `a1b2c3d4e5f6` | `manifest.json` (`model_version recovery-v1`, `feature_schema v1`, `fingerprint` sha256, `metrics` incl. Brier, `synthetic_data_notice`); `Decision.policy_mode/model_version/fingerprint/friction_*` + `alternatives[_provenance]` + `AuditEvent(adaptive_decision)` + `/health` + `/cases/{id}` |
| One canonical feature pipeline | **IMPLEMENTED / VERIFIED** | `app/ml/features.py`, `app/ml/scorer.py`, `app/ml/train.py`, `tests/test_adaptive_policy.py::test_train_serve_parity` | Same `FEATURE_COLUMNS` + `validate_feature_row` for train, offline eval, live scoring; hour/day_of_week from decision clock; `LIVE_AVAILABLE` only, no leakage |
| Synthetic training data generator | **IMPLEMENTED / SYNTHETIC_ONLY** | `app/ml/synthetic_history.py:45` | Uniform action sampling to expose all (category, action) pairs to the model; still synthetic — see `ML_AND_EVALUATION.md` |
| Friction-aware experiment runner | **IMPLEMENTED / VERIFIED** | `app/services/experiment_runner.py:47`, `tests/test_experiments.py`, `scripts/evaluate_policies.py` | Baseline vs balanced adaptive with `common-random` scenarios; per-arm `friction_score`, `contact_rate`, `recovered_per_contact`, `utility`, `waits`/`payment_links`/`ptps` breakdown |
| Experiment persistence / API / CSV export | **IMPLEMENTED / VERIFIED** | `app/routers/experiments.py:22`, `tests/test_experiments.py:91` | `ExperimentCase` rows keyed by `run_id`; `GET /experiments/{id}/export.csv` returns header + `count*2` rows; summary now includes `friction_score`/`utility` per arm |
| React merchant console (observability) | **IMPLEMENTED / VERIFIED** | `frontend/src/App.tsx` (Overview/Cases/Experiments/System), `frontend/src/components/**`, `backend/app/services/explainability.py`, `backend/app/routers/dashboard.py`, `backend/app/routers/cases.py` (enriched), `tests/test_observability.py` | Dashboard summary (revenue at risk/recovered, state/case counts, PTP, policy/model), filterable case list (state/category/action/policy/search + pagination), full case detail (failure → guardrails → Decision Inspector with P/EV/friction/utility → timeline → provider truth → drafts → PTP → provenance), Revenue-vs-friction Pareto & action distribution, deep-link `?case=<id>`, loading/error/empty states, responsive; `GET /dashboard/summary` + enriched `GET /cases/{id}` (timeline, decision_inspectors, provider_truth) |
| Dashboard summary read-model | **IMPLEMENTED / VERIFIED** | `app/routers/dashboard.py`, `app/services/explainability.py`, `tests/test_observability.py` | `GET /dashboard/summary` derives revenue at risk (open), recovered (RECOVERED), by_state/by_category, active PTPs, policy/model/fingerprint, friction, queue, Razorpay (SIMULATED vs TEST MODE), LLM — PostgreSQL is truth, no invented values |
| Case list operational view | **IMPLEMENTED / VERIFIED** | `app/routers/cases.py`, `frontend/src/components/CaseList.tsx` | Server-side filters (state, failure_category, chosen_action, policy_mode, search by payment/case/link, limit/offset) with enriched columns (amount/state/failure/action/policy/friction/latest Action/Link/PTP/updated) — no browser mass filtering |
| Decision explainability | **IMPLEMENTED / VERIFIED** | `app/services/explainability.py:normalize_decision`, `app/routers/cases.py`, `frontend/src/components/CaseDetail.tsx` | Candidate comparison per Decision (allowed, guardrail reason, P, EV, cost, friction, penalty, utility, selected) — missing adaptive fields show “unavailable”/“Not recorded”, never fabricated; baseline shows no ML probabilities (correct) |
| Guardrail visibility | **IMPLEMENTED / VERIFIED** | `app/services/explainability.py:guardrail_visibility`, `frontend/src/components/CaseDetail.tsx` | Current contact limits, 7-day cap, cooldown, amount cap, attempt limit are recomputed at read time and labeled current; persisted `Decision.guardrails_applied` remains decision-time evidence |
| Friction visibility | **IMPLEMENTED / VERIFIED** | `app/services/explainability.py:friction_breakdown`, `frontend/src/components/CaseDetail.tsx` | Current base friction + current contact increment is labeled a read-time surface; persisted candidate friction/utility remains decision-time evidence in Decision Inspector |
| Recovery timeline | **IMPLEMENTED / VERIFIED** | `app/services/explainability.py:build_timeline`, `frontend/src/components/CaseDetail.tsx` | Chronological from PaymentEvent+Decision+Action+Message+PTP+AuditEvent (not persisted duplicate), ordered, with audit fallbacks; no fake events |
| Payment Link / provider truth | **IMPLEMENTED / VERIFIED** | `app/services/explainability.py:provider_truth_summary`, `frontend/src/components/CaseDetail.tsx` | SIMULATED LINK vs RAZORPAY TEST MODE, plink_* id, reference_id, short_url (safe), exact-Action `action_reconciled` evidence, status — never claims LIVE production |
| PostgreSQL as authoritative state | **IMPLEMENTED / VERIFIED** | migration `a1b2c3d4e5f6`, `app/models.py:Decision` provenance, `app/services/temporal_runtime.py` | `Action` owns schedule/claim/attempts; `Decision` owns `policy_mode`/`model_version`/`fingerprint`/`friction_*`; Redis loss is repaired by reconciliation |
| Redis/RQ delayed-action runtime | **IMPLEMENTED / VERIFIED** | `app/services/task_queue.py`, `app/jobs.py`, `app/worker.py`, `scripts/reconcile_actions.py` | Worker process and `--with-scheduler` must be running; Windows requires `app.worker.WindowsWorker` |
| Real inbound Razorpay webhooks (production live-money) | **NOT IMPLEMENTED** — production explicitly rejected | `app/services/razorpay_client.py` rejects non-`rzp_test_*` keys | Webhook verification is provider-shape-agnostic; Test Mode integration is implemented, but current manual provider smoke is pending |
| Demo seed & scenarios | **IMPLEMENTED / VERIFIED** | `scripts/seed_demo.py`, `tests/test_observability.py`, `docs/DEMO_SCRIPT.md` | Five core rows always: A contact→PTP, B link→AWAITING_OUTCOME, C WAIT + C_NATIVE, E injection→HUMAN_REVIEW. With a trained local model, D persists `policy_mode=adaptive` and F persists `policy_mode=shadow` (seven rows across A-F). Idempotent, demo-only reset via `POST /admin/demo-reset` or CLI, no external API by default; offline SIMULATED fallback at `docs/OFFLINE_DEMO.md` |
| Deployment (Docker, prod compose) | **IMPLEMENTED / VERIFIED** | Dockerfiles, `.dockerignore` files, and `docker-compose.prod.yml` | API and worker share one image; build context excludes `.env`, virtualenvs, and host model artifacts; image trains and validates its own model; Postgres/Redis dependencies use native healthchecks |
| Health vs readiness | **IMPLEMENTED / VERIFIED** | `app/routers/health.py` (`/health` liveness, `/ready` gated, `/live` pure) | `/health` sanitized, `/ready` gates DB+Redis (503 when not ready), LLM/Razorpay optional never degrades; no secrets leaked |
| Public-demo safety | **DEMO-GRADE / VERIFIED** | `app/core/demo_auth.py` (`DEMO_ADMIN_TOKEN_ENABLED`), `tests/test_release_hardening.py` | Sensitive `GET /cases*`, `/dashboard/summary`, `/experiments*` plus operator mutations are gated when enabled; root/health/readiness remain public; webhook remains HMAC-only; token stored in sessionStorage |
| Startup validation | **IMPLEMENTED / VERIFIED** | `app/core/config.py:validate_startup_config`, `app/main.py` | Fails fast for incomplete test-mode creds, live-key accident, unknown policy; dev defaults safe |
| CORS | **IMPLEMENTED / VERIFIED** | `app/core/config.py:parse_frontend_origins`, `app/main.py` | Explicit origins use credentials; wildcard alone disables credentials; mixed wildcard and explicit origins fail startup |
| Structured logging + correlation | **IMPLEMENTED / VERIFIED** | `app/core/logging_config.py`, `app/core/middleware.py` (`X-Request-ID`) | JSON structured, redacts secrets, never logs raw payloads/LLM prompts |
| Failure injection | **DEV-ONLY / VERIFIED** | `app/services/failure_injection.py` (`FAILURE_INJECTION_ENABLED` gate) | Header `X-Failure-Inject` with bounded cases; never silently active in prod |
| Experiment guard | **IMPLEMENTED / VERIFIED** | `app/routers/experiments.py` (`EXPERIMENT_MAX_COUNT=1000`, default 100) | Prevents accidental 5000-case freeze; synthetic banner everywhere |
| Frontend polling & failure UX | **IMPLEMENTED / VERIFIED** | `frontend/src/App.tsx` (30s dashboard/health + 10s active case), `frontend/src/api.ts` (token) | No `undefined`/`NaN`/blank page; honest error states |

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

1. **Worker/reconciliation processes are operational dependencies** - committed actions remain safe if they stop, but execution is delayed until a worker and periodic reconciliation resume (reconcile via `python scripts/reconcile_actions.py` or prod compose healthcheck).
2. **Provider exactly-once remains bounded by provider primitive** - Razorpay enforces uniqueness on `reference_id` (400 on duplicate) and supports `list?reference_id=` filtering, which this stage uses for reconciliation. A provider crash between `POST /payment_links` commit on Razorpay's side and response transmission still requires one extra `GET /payment_links?reference_id=` to discover the already-created link. DB claim + `reference_id` + reconciliation reduce ambiguity to a single validated lookup, but true exactly-once still depends on Razorpay's documented uniqueness guarantee, not on DB idempotency alone.
3. **`DECISION_READY` is vestigial in reply handling** - it is an ephemeral policy state.
4. **Customer identity is synthetic** - `Customer` rows are not enriched from Razorpay customer/subscription entities.
5. **Contact fatigue is per-case only** - limits are not aggregated per customer or merchant.
6. **Database timestamps remain naive UTC** - merchant-local conversion is explicit only for PTP business dates.
7. **Model artifact is derived** - source runs must run `python -m app.ml.train`; container builds always train and validate their own artifact after excluding any host artifact from the build context.
8. **Demo-grade auth only** - webhook authenticity relies on signature and event ID; sensitive operator reads and mutations use optional `DEMO_ADMIN_TOKEN` (not production IAM). See `docs/PUBLIC_WEBHOOK.md`.
9. **Case-detail renderer was built in prior stage** — `frontend/src/components/CaseDetail.tsx` renders `action_reconciled` / `payment_link_reconciliation_*`, shadow, fallback, and provider truth distinctly; remaining debt is pre-existing (see below).

---

## Next planned subsystem

**SUBMISSION FINALIZATION** — the next and final stage after this release-hardening stage.

Release hardening is now **IMPLEMENTED**: liveness/readiness, demo safety (optional token, webhook exempt), startup validation, CORS, structured logging + correlation, bounded experiments, failure injection (dev-only), queue/worker orphan recovery, deployment artifacts (Dockerfiles + `docker-compose.prod.yml`), and reproducible judge demo (`docs/DEMO_SCRIPT.md`, `docs/OFFLINE_DEMO.md`, `docs/PUBLIC_WEBHOOK.md`) are live and verified. The system now shows:

    Razorpay failure → diagnosis → guardrails → baseline/adaptive decision → friction-adjusted candidate comparison → durable Action → temporal execution → Razorpay reconciliation → customer draft / PTP → provider-authoritative recovery → dashboard explains everything

with guardrails, shadow/fallback, provider truth, and synthetic `SYNTHETIC SIMULATION` labeling visible. See `docs/DECISIONS.md` and `docs/ML_AND_EVALUATION.md` for remaining synthetic-evaluation and real-outcome learning gaps.

Remaining gaps for submission stage: final README polish, architecture graphic, screenshots, demo video script, hackathon submission text, pitch, judging Q&A.

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
