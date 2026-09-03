# RecoveryOS — Development Guide

> How to change the system without breaking what is verified. Read `docs/CURRENT_STATE.md` first for status, then `ARCHITECTURE.md` for component boundaries.

---

## Repository structure

```
recoveryos/
├── README.md                 # concise entry point → links outward
├── ARCHITECTURE.md           # canonical design with [STATUS] labels
├── docs/
│   ├── CURRENT_STATE.md      # MOST IMPORTANT living doc — update first
│   ├── SYSTEM_FLOWS.md       # exact flows derived from orchestrator/policy/executor
│   ├── RUNBOOK.md            # Windows-friendly operator commands
│   ├── ML_AND_EVALUATION.md  # training, simulator, circularity, tradeoff
│   ├── INTEGRATIONS.md       # Razorpay / LLM / Redis-RQ — real vs simulated
│   ├── DEVELOPMENT.md        # this file
│   └── DECISIONS.md          # ADRs with status
├── docker-compose.yml        # local Postgres (authoritative) + Redis (RQ transport)
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI app + router registration
│   │   ├── models.py         # schema shape source of truth
│   │   ├── jobs.py           # ID-only RQ entry point
│   │   ├── worker.py         # Windows-compatible RQ worker class
│   │   ├── core/
│   │   │   ├── config.py     # env-driven Settings (never touch os.environ elsewhere)
│   │   │   ├── database.py   # engine / SessionLocal / get_db
│   │   │   ├── security.py   # verify_signature (HMAC-SHA256 on raw bytes)
│   │   │   └── time.py       # utc_now() — naive UTC for legacy schema
│   │   ├── services/
│   │   │   ├── orchestrator.py        # owns every RevenueCase.state mutation
│   │   │   ├── failure_diagnosis.py   # 8-category deterministic taxonomy
│   │   │   ├── policy_engine.py       # guardrails + baseline; ACTION_TO_STATE; CATEGORY_ACTION_PREFERENCE
│   │   │   ├── razorpay_client.py     # Payment Links — simulated or Test Mode
│   │   │   ├── action_executor.py     # SCHEDULED → EXECUTED / FAILED (+ CustomerMessage)
│   │   │   ├── ml_policy.py           # friction-aware adaptive — live via dispatcher, guarded + manifest
│   │   │   ├── llm_client.py          # draft + extract — simulated or Anthropic
│   │   │   ├── ptp_extractor.py       # validates extraction before PromiseToPay
│   │   │   ├── ptp_followup.py        # exact linked-promise completion
│   │   │   ├── task_queue.py          # after-commit RQ publishing
│   │   │   ├── temporal_runtime.py    # claims, execution, retry, reconciliation
│   │   │   └── experiment_runner.py   # persisted matched-scenario experiments
│   │   ├── ml/
│   │   │   ├── ground_truth.py        # hand-set P(recovery) simulator + modifiers
│   │   │   ├── synthetic_history.py   # training DataFrame generator
│   │   │   ├── costs.py               # per-action INR proxies
│   │   │   ├── train.py               # HistGB pipeline → artifacts/model.joblib
│   │   │   ├── scorer.py              # P̂·amount − cost ranking
│   │   │   └── artifacts/             # gitignored model.joblib
│   │   └── routers/
│   │       ├── health.py              # GET /health, GET /
│   │       ├── cases.py               # GET /cases, GET /cases/{id}, POST /cases/{id}/customer-reply
│   │       ├── webhooks.py            # POST /webhooks/razorpay
│   │       └── experiments.py         # POST/GET /experiments, GET /{id}/export.csv
│   ├── alembic/versions/     # 4 revisions — only way schema changes
│   ├── scripts/
│   │   ├── send_test_webhook.py       # signed webhook sender (local testing)
│   │   ├── run_synthetic_batch.py     # 100-case policy health check
│   │   ├── evaluate_policies.py       # baseline vs ML script (non-persisted)
│   │   ├── reconcile_actions.py       # repair claims and republish DB work
│   │   └── process_followups.py       # compatibility reconciliation entry
│   ├── tests/                # hermetic suite; canonical count in CURRENT_STATE.md
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── App.tsx
    │   ├── ExperimentPanel.tsx
    │   ├── api.ts
    │   └── main.tsx
    ├── package.json
    └── .env.example
```

---

## Branch expectations

- Active branch for this pass: `feat/payment-link-reconciliation`.
- Do **not** switch / merge / rebase / stash / commit from documentation passes — leave changes uncommitted.
- Feature work branches from the same baseline must keep `docs/CURRENT_STATE.md` as its first doc update when behavior changes.

---

## Environment files

| File | Tracked? | Purpose |
|------|----------|---------|
| `backend/.env.example` | Yes | Every supported `KEY=` with sane defaults; docs reference this, never a secret |
| `backend/.env` | No (`.gitignore:16`) | Developer-local real values; read by `pydantic-settings` (`config.py:10 env_file=.env`) |
| `frontend/.env.example` | Yes | `VITE_API_BASE_URL=http://localhost:8000` |
| `frontend/.env` | No | Developer-local override |

**Canonical variable reference lives in `ARCHITECTURE.md` → Razorpay/LLM sections and `docs/INTEGRATIONS.md`.** Minimal set:

| Variable | Required | Default | Enables external call? |
|----------|----------|---------|------------------------|
| `APP_NAME` | No | `RecoveryOS` | No |
| `ENV` | No | `dev` | No (tests set `test`) |
| `DATABASE_URL` | Yes for real run | `postgresql+psycopg2://recoveryos:recoveryos@localhost:5432/recoveryos` | No |
| `REDIS_URL` | When queue enabled | `redis://localhost:6379/0` | Redis transport only |
| `RQ_QUEUE_NAME` | No | `recoveryos` | Selects worker queue |
| `TASK_QUEUE_ENABLED` | No | `true` | Gate for Redis publishing/health checks |
| `WAIT_DELAY_SECONDS` | No | `3600` | Generic wait deadline |
| `NATIVE_RETRY_DELAY_SECONDS` | No | `86400` | Native retry deadline |
| `ACTION_MAX_ATTEMPTS` | No | `3` | Bounded provider attempts for new actions |
| `ACTION_RETRY_DELAY_SECONDS` | No | `60` | Exponential retry base |
| `ACTION_CLAIM_TIMEOUT_SECONDS` | No | `300` | Abandoned `EXECUTING` lease threshold |
| `ACTION_JOB_TIMEOUT_SECONDS` | No | `30` | RQ execution timeout |
| `MERCHANT_TIMEZONE` | No | `Asia/Kolkata` | PTP date validation/deadline zone |
| `RAZORPAY_API_ENABLED` | No | `false` | **Gate** — live Payment Links only when `true` + valid key |
| `RAZORPAY_KEY_ID` | When enabled | `""` | Only with gate; `rzp_test_*` enforced |
| `RAZORPAY_KEY_SECRET` | When enabled | `""` | Only with gate |
| `RAZORPAY_WEBHOOK_SECRET` | Yes | `""` → **fails closed** (signature never passes) | No |
| `LLM_API_ENABLED` | No | `false` | **Gate** — Anthropic only when `true` |
| `LLM_PROVIDER` | No | `anthropic` | Non-`anthropic` raises `LLMAPIError` when enabled |
| `LLM_API_KEY` | When enabled | `""` | Only with gate |
| `LLM_MODEL` | No | `claude-3-5-haiku-latest` | Model id |
| `LLM_TIMEOUT_SECONDS` | No | `10` | Bounded LLM timeout |
| `LLM_MAX_RETRIES` | No | `2` | Bounded retries (transient only) |
| `LLM_MESSAGE_DRAFT_ENABLED` | No | `true` | Sub-gate for drafting |
| `LLM_PTP_EXTRACTION_ENABLED` | No | `true` | Sub-gate for extraction |
| `FRONTEND_ORIGIN` | No | `http://localhost:5173` | CORS `allow_origins` (comma-separated for deploy) |
| `VITE_API_BASE_URL` | No | `http://localhost:8000` | Frontend fetch base |
| `DEMO_ADMIN_TOKEN_ENABLED` | No | `false` | When true, sensitive operator reads and mutations require `X-Demo-Admin-Token`; root/health/readiness and webhook exempt |
| `DEMO_ADMIN_TOKEN` | When enabled | `""` | Bearer for demo ops — never baked into frontend build |
| `FAILURE_INJECTION_ENABLED` | No | `false` | Dev/test header `X-Failure-Inject` — never silently active |
| `EXPERIMENT_MAX_COUNT` | No | `1000` | Max `count` for `POST /experiments` |
| `ENFORCE_TEST_MODE_ONLY` | No | `true` | Reject `rzp_live_*` keys |

Do not document real secret values. The `.env.example` values are the only values that may appear in docs.

---

## Migrations — the only way schema changes

- Schema is **locked** to `app/models.py` plus `backend/alembic/versions/*.py`. Never hand-edit the Postgres schema outside Alembic.
- Workflow:

```bash
# from backend/
# after changing app/models.py:
alembic revision --autogenerate -m "add xyz"
# review the generated file — SQLite-autogenerated UUID type noise is known
# (see d3e3dbb702c4 / 197e63dfda6f — NUMERIC vs UUID spurious diffs; strip them)
alembic upgrade head
python -m pytest -q
```

- CI verifies `alembic upgrade head` against `sqlite:///./ci_migration.db` (`ci.yml`). Local Postgres verification is in `docs/RUNBOOK.md`.

---

## Test isolation

Non-negotiable — see `tests/conftest.py`:

- `ENV=test`, `DATABASE_URL=sqlite:///<tempfile>/test.db` (per-session tmpdir, removed after).
- `TASK_QUEUE_ENABLED=false` and an inert loopback `REDIS_URL`; normal tests never require Redis.
- `RAZORPAY_API_ENABLED=false`, `LLM_API_ENABLED=false`; fake keys so "credentials present" branches are exercised as `simulated` without network.
- `socket` guard: only `127.0.0.1` / `::1` permitted; anything else raises `AssertionError: automated tests must not access external networks`.
- Uses `Base.metadata.create_all` / `delete()` instead of `alembic upgrade` for speed; schema is still verified by the explicit CI migration step.
- Fixture `trained_model_path(tmp_path_factory)` trains a **separate** ephemeral model for `test_experiments` / `test_ml_*` so CI never depends on `artifacts/model.joblib`.

Before adding a test that needs the trained model, accept a `trained_model_path` argument or use the mock pattern in `test_experiments.py:147`.

---

## How to add a new recovery action

1. Add to `policy_engine.ALL_ACTIONS` (`policy_engine.py:240`) **and** `ml/costs.py:ACTION_COST` (cost == missing raises in `scorer.rank_actions`).
2. Add `ACTION_TO_STATE[action] = …` mapping (`policy_engine.py:41`) — every state must exist or the orchestrator invariant breaks.
3. Decide if the baseline prefers it: add to `CATEGORY_ACTION_PREFERENCE[category]` in preference order. If it is always guardrail-filtered, add its check to `_check_action_allowed`.
4. Decide if it needs execution: add delayed/internal types to `task_queue.QUEUEABLE_ACTION_TYPES` and a branch in `temporal_runtime.process_action`; add provider-call types to `action_executor.EXECUTABLE_ACTION_TYPES` and `perform`. Provider calls must occur after the claim commit, never in a request transaction.
5. **Provider side effects must use a stable provider identity** — `razorpay_client.build_provider_reference(Action.id)` is canonical; every external create must send `reference_id=Action.id` plus `notes={recoveryos_case_id, recoveryos_action_id}` and be sanitized via `_sanitize_link()`.
6. **External calls must classify ambiguity:** `Timeout`/`NetworkError`/`5xx`/`429`/duplicate `reference_id` → `RazorpayAmbiguousError` → `find_payment_link_for_action` reconciliation before any retry; definite `4xx` → `RazorpayAPIError` → bounded retry. Never blindly recreate on ambiguous outcome. See `razorpay_client.py` and `temporal_runtime._handle_ambiguous_payment_link`.
7. **Reconciliation is mandatory:** stale `EXECUTING` payment-link claims must `GET /v1/payment_links?reference_id=Action.id` outside any txn, validate `reference_id`/`amount`/`currency`/`notes`/`status`, and either adopt via canonical `apply_success(_reconciled=true)` → `action_reconciled` or route to `HUMAN_REVIEW` on mismatch. Add manual repair in `scripts/reconcile_payment_links.py`.
8. Update ML ground truth if appropriate: `BASE_PROBABILITY[(category, action)]` in `app/ml/ground_truth.py:33`; out-of-table pairs fall back to `FALLBACK_PROBABILITY`, which may not be what you want.
9. Update `CONTACT_ACTIONS` if the action counts as contact/friction (`policy_engine.py:38` also drives `ptp_followup` and experiment counting).
10. Add tests: policy choice/guardrails plus duplicate delivery, terminal-state stale work, retry exhaustion, expired-claim reconciliation, provider ambiguity/reconciliation (adopt/absent/mismatch), and ID-only queue payloads in `test_temporal_runtime.py` / `test_payment_link_reconciliation.py`.

---

## How to add a new webhook event

1. In `app/services/orchestrator.py`, add to the appropriate set (`FAILURE_EVENTS`, `RECOVERY_EVENTS`, `DISPUTE_EVENTS`, or a new `PAYMENT_LINK_PAID_EVENT` analogue) and handle it in `handle_event` before the fallback `return None`.
2. Add explicit event-to-wrapper extraction. Never iterate payload wrapper order when an event can contain payment, subscription, link, and dispute entities together.
3. Decide state eligibility. Current rules: `STOPPED` can still recover; `RECOVERED`/`DISPUTED` ignore recovery; dispute always overrides recovery.
4. Add audit events; treat `PaymentEvent` as append-only.
5. Add `send_test_webhook.py` helper if manual testing is useful (see `payment_link.paid` payload in `tests/test_webhooks.py:181`).
6. Add `tests/test_webhooks.py` coverage including `400` on malformed body / `duplicate_event` idempotency.
7. Update `docs/SYSTEM_FLOWS.md` with the new flow and `ARCHITECTURE.md` Razorpay boundary.

---

## How to update state transitions

1. Change only the owning service: `orchestrator.py` for event/reply transitions, `policy_engine.py` for decisions, and `temporal_runtime.py`/`action_executor.py`/`ptp_followup.py` for worker transitions. Routers and ML code must not mutate case state.
2. Update `REPLYABLE_STATES` / `DIAGNOSABLE_STATES` / `TERMINAL_STATES` sets where transition eligibility changes.
3. Add/correct `AuditEvent(event, detail)` for every new mutation — these are the only way the dashboard/detail endpoint and inspectors explain "why".
4. Update `ARCHITECTURE.md` state machine diagram and table (`State inventory`, `Known deviation`), and `docs/SYSTEM_FLOWS.md` row for every affected flow.
5. Add migration if the transition needs a persisted column (e.g., a new `scheduled_for` semantic on `Action`).

### Temporal runtime invariants

- PostgreSQL is authoritative; Redis payloads contain only `action_id`.
- Queue publishing/reconciliation must join the case and require `source=razorpay`; experiment actions are never executable work.
- Publish only after the action transaction commits. Queue failure must leave recoverable `SCHEDULED` state.
- Lock the case before conditionally claiming an action to preserve lock ordering.
- Commit `EXECUTING` before any Redis/provider/LLM network call; finalize in a new transaction — **provider HTTP is never inside a long DB transaction** (`temporal_runtime` snapshots the case and closes the claim txn before `action_executor.perform`; reconciliation does `GET /v1/payment_links?reference_id=` outside any txn before finalize).
- Recheck terminal state and supersession during finalization, and re-check `case.state` before provider reconciliation (spec 38).
- Persist sanitized failure categories, not raw provider exceptions — use `_sanitize_link()` and truncated `sanitize_provider_error()`.
- Any schema or status change must update `scripts/reconcile_actions.py`, `scripts/reconcile_payment_links.py`, `GET /cases/{id}`, runtime tests, and canonical docs.

### Future policy/model change rules

- **Feature schema compatibility:** `app/ml/features.py:FEATURE_SCHEMA_VERSION` gates `scorer._load` + manifest validation; bumping it requires retraining and a new `model_version`. Never silently add a feature to `train.py` without updating `features.py` and `MANIFEST_VERSION`.
- **Manifest versioning:** `app/ml/manifest.py:MANIFEST_VERSION` + `MODEL_VERSION` must be bumped when feature set, action_set, or training semantics change; `scorer.get_model_info` must reflect it for `/health` and `Decision` provenance.
- **Guardrail reuse:** Adaptive must call `policy_engine.check_action_allowed` and semantic `WAIT_FOR_NATIVE_RETRY` filter — never duplicate guardrails in `ml_policy.py`.
- **Safe fallback:** Any `scorer` exception must emit `adaptive_fallback` audit and delegate to `policy_engine.decide` in the same transaction; `RECOVERY_POLICY` is startup-loaded, global-at-decision-time, and requires restart to change.
- **Shadow-first rollout:** New adaptive logic should be validated in `RECOVERY_POLICY=shadow` (baseline executes, `shadow_adaptive_recommendation` audited) before `adaptive` promotion.
- **No direct side effects from ML:** `ml_policy` may only rank; `action_executor` remains the only caller of `razorpay_client`/`llm_client` via `temporal_runtime`.

### LLM engineering invariants (this stage)

- **LLM never chooses financial actions** — deterministic/guardrails or guarded adaptive remain sole controllers; LLM is downstream support only (draft / structured extraction).
- **Customer input is untrusted** — prompt architecture separates system/task instructions from `<untrusted_customer_text>`; `_detect_injection` downgrades model output regardless of intent or amount; deterministic `ptp_extractor.validate_promise` authoritative.
- **Structured output is validated** — `_validate_structured_output` checks enum, amount type, date format, confidence bounds; direct malformed output raises `LLMInvalidResponseError`, and only `extract_with_fallback` converts it to deterministic fallback provenance.
- **No prompt chaining into payment state** — customer text cannot set `RECOVERED`; only Razorpay webhook truth does; `payment_claim` → `DISPUTED`/`HUMAN_REVIEW`.
- **Deterministic fallback required** — `LLM_API_ENABLED=false` fully operational; `draft_with_fallback` / `extract_with_fallback` ensure `DRAFT` / `uncertain` on timeout/429/500/invalid; no case stuck because LLM down.
- **DB errors not swallowed** — provider/LLM errors may fallback, DB write failures propagate; `except Exception` around DB operations must not convert to LLM fallback.
- **Prompts versioned** — `PTP_EXTRACTION_PROMPT_VERSION=ptp-v1`, `MESSAGE_DRAFT_PROMPT_VERSION=message-v1`, schema versions in `llm_client.py:43`, stored in `CustomerMessage`/`PromiseToPay` provenance and audit detail; never scattered.
- **Provider calls mocked in tests** — `httpx.post` monkeypatched, `conftest.py` socket guard loopback-only, `TASK_QUEUE_ENABLED=false`, `LLM_API_ENABLED=false` default; fake credentials never hit network.
- **Payment Link URLs provider-authoritative** — LLM generates `[[PAYMENT_LINK]]` placeholder; all model-provided HTTP(S) URLs are stripped and `action_executor` inserts the exact `short_url` once; amount/discount/fee never invented — authoritative fields rendered deterministically outside LLM.
- **Omitted financial facts are not invented** — `promised_amount` is only what customer explicitly said; omitted → `HUMAN_REVIEW` with `amount_method=customer_explicit` (current rule); `[[PAYMENT_LINK]]` substitution uses only authoritative provider state.
- **Customer claims do not establish payment truth** — `RECOVERED` only via `payment.captured`/`payment_link.paid`.

---

## How to update schema

1. Edit `app/models.py` (source of truth for model shape).
2. `alembic revision --autogenerate -m "…"`.
3. Strip spurious `alter_column` UUID noise (see existing migrations' comments).
4. `alembic upgrade head` locally (Postgres) and via `DATABASE_URL=sqlite:///./ci_migration.db python -m alembic upgrade head` for CI parity.
5. Update `ARCHITECTURE.md` ER diagram and Table descriptions.

---

## How ML artifacts are generated

| Artifact | How | Where | Tracked? |
|----------|-----|-------|----------|
| `model.joblib` | `python -m app.ml.train [--n 30000] [--seed 42]` from `backend/` | `backend/app/ml/artifacts/` | No — `.gitignore:8`; CI trains from scratch each run |

Details, cost proxies, and failure modes: `docs/ML_AND_EVALUATION.md`.

---

## Frontend

```bash
cd frontend
Copy-Item .env.example .env   # or: copy / cp .. depending on shell — see docs/RUNBOOK.md
npm install
npm run lint    # oxlint
npm run build   # tsc -b && vite build → dist/
npm run dev     # Vite dev server on 5173; expects backend on 8000
```

API client: `src/api.ts` — typed `HealthResponse`, `DashboardSummary`, `RevenueCase`, `CaseDetail`, `DecisionInspector`, `TimelineEvent`, `ArmSummary`, `ExperimentSummary`. `frontend/src/components/**` drives Overview / Cases / Experiments / System with `GET /dashboard/summary`, `GET /cases` (filtered), `GET /cases/{id}` (enriched with timeline/decision_inspectors/provider_truth), and `?case=<id>` deep-linking. `ExperimentPanel.tsx` shows revenue-vs-friction Pareto & action distribution.

### Observability / read-model invariants (this stage)

- **PostgreSQL remains truth.** `explainability.py` only derives from `RevenueCase`+`Decision`+`Action`+`AuditEvent`+`CustomerMessage`+`PromiseToPay`+`PaymentEvent`. No second timeline table. Historical missing provenance renders `null`/`unavailable`, never fabricated (frontend never invents controller scores).
- **Frontend never invents financial truth.** Revenue at risk/recovered, state counts, friction, utility, provider truth come from backend read-model or deterministic computation; synthetic metrics remain labeled `SYNTHETIC SIMULATION`.
- **Customer drafts never shown as delivered.** All outbound `CustomerMessage` are `status=DRAFT` / `MANUAL_ONLY` until a real transport exists; dashboard shows `DRAFT / NOT SENT`.
- **No LLM-generated controller explanation.** All human-readable explanations of controller behavior are deterministic from persisted fields (`CATEGORY_ACTION_PREFERENCE`, `alternatives`, `decision.explanation`, guardrail config). LLM is downstream support only (draft / PTP extraction with placeholder safety and deterministic validation). No new AI, no RAG, no agent — this stage only exposes existing intelligence.

---

## Documentation update requirements — which file for what

| Change type | Must update |
|-------------|-------------|
| Architecture / component boundaries / state model | `ARCHITECTURE.md` |
| Status of what is implemented vs simulated vs planned | `docs/CURRENT_STATE.md` (first) |
| Workflow / state machine / side-effect behavior | `docs/SYSTEM_FLOWS.md` |
| Commands / env vars / troubleshooting / Windows vs CMD syntax | `docs/RUNBOOK.md` |
| Model, simulator, costs, metrics, evaluation, tradeoff, calibration plan | `docs/ML_AND_EVALUATION.md` |
| Provider shape, gates, simulation, delivery, payload samples | `docs/INTEGRATIONS.md` |
| Repo layout, branches, migrations, tests, how-to guides | `docs/DEVELOPMENT.md` (this file) |
| Reasoning behind a principle or constraint | `docs/DECISIONS.md` |
| New top-level capability (judge-visible in 2 minutes) | `README.md` (keep concise — link outward) |

**README should stay concise and link outward.** It is the only document a judge reads fully; the rest are canonical references.

---

## Definition of Done for every future subsystem

Every implementation phase must ship:

1. **Code** — feature under `backend/app/**` or `frontend/src/**`.
2. **Tests** — hermetic; no external network; covers happy + known bad-input + idempotency; lives under `backend/tests/**` and passes on the temp SQLite DB.
3. **Migrations** — if schema changed, one `alembic revision` applied to `app/models.py`.
4. **README** — only if entry-point behavior changes (new judge-visible capability, new disclaimer, new quick-start path).
5. **ARCHITECTURE** — if architecture changes (new component, state, transition, DB truth guarantee, diagram).
6. **`CURRENT_STATE.md`** — capability-matrix row updated, runtime-reality table updated if delayed/side-effect semantics changed, technical debt / next subsystem refreshed, doc legend honoured.
7. **`SYSTEM_FLOWS.md`** — flow row added/updated with trigger/persisted entities/state change/side effects/failure behavior/current limitation.
8. **`RUNBOOK.md`** — if any command, `working_directory`, or env var changed (plus Windows `--` vs `-` and PowerShell vs CMD splits where syntax differs).
9. **`INTEGRATIONS.md`** — if any external provider behavior changed (gates, payloads, test-mode gates, delivery transports).
10. **`ML_AND_EVALUATION.md`** — if anything touching model/features/costs/evaluation/revenue-friction tradeoff changed (with circularity note and synthetic vs production disclaimer).
11. **Final validation results** — `python -m pytest --collect-only -q` still reports the current count, `npm run build` and `npm run lint` pass, `alembic upgrade head` (SQLite) still passes; paste doctext in the phase report.

A phase is **not complete** if its docs still describe the old behavior.

---

## Source-of-truth / anti-drift rule

> Documentation is part of the implementation.
>
> A subsystem is not considered complete until its canonical documentation no longer describes the previous behavior. Every future prompt report must identify: **which doc files changed**, **which were intentionally left unchanged and why**, and **the reason** for each (e.g. "no new provider shape → `INTEGRATIONS.md` unchanged").
>
> Avoid repeating volatile values. The test count is authoritative in `docs/CURRENT_STATE.md` — do not duplicate it in README/ARCHITECTURE/RUNBOOK unless you are prepared to bump all of them every run.

---

## Governance — who owns what

```
README.md               — maintained by whoever ships a judge-visible change; reviewed against "2-minute" test.
ARCHITECTURE.md         — owned by the phase lead for architectural changes; reviewed for diagram ↔ code alignment.
docs/CURRENT_STATE.md   — owned by every phase; mandatory first update when the matrix drifts.
docs/SYSTEM_FLOWS.md    — co-owned by orchestrator/policy/executor leads; updated per flow.
docs/RUNBOOK.md         — owned by DevEx / reliability; every command must be copy-pasteable on Windows.
docs/ML_AND_EVALUATION.md — owned by ML lead; must remain scientifically honest (circularity, tradeoff).
docs/INTEGRATIONS.md    — owned by integration leads (Razorpay/LLM/Queue); real vs simulated must be explicit.
docs/DEVELOPMENT.md     — owned by infra/DX; how to change the system safely.
docs/DECISIONS.md       — append-only ADRs; phase lead proposes, reviewers approve, never rewrite past.
```

---

## Checklist before opening a PR

- [ ] Tests pass: `cd backend && python -m pytest -q` (no external network or Redis required; includes LLM typed errors, placeholder safety, PTP validation, drafting fallback, controller isolation, DB boundary, provenance, demos). The current count is recorded only in `docs/CURRENT_STATE.md`.
- [ ] Frontend passes: `cd frontend && npm run lint && npm run build` (policy/model/friction/llm visible in header; `DRAFT / NOT SENT` banner).
- [ ] Migrations verify: `cd backend && DATABASE_URL=sqlite:///./ci_migration.db python -m alembic upgrade head` (head `c9d0e1f2a3b4` LLM provenance; downgrade/upgrade fresh verified).
- [ ] No secrets introduced into diff (`backend/.env`, `frontend/.env` are gitignored — check `.env.example` only); `httpx` calls are mocked in tests, simulation remains default.
- [ ] Documentation updated per table above; `git diff --check` (whitespace) clean.
- [ ] Report lists docs changed / intentionally unchanged / reason.
