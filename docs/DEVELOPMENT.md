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
├── docker-compose.yml        # local Postgres (required) + Redis (reserved)
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI app + router registration
│   │   ├── models.py         # 9 tables — schema shape source of truth
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
│   │   │   ├── ml_policy.py           # adaptive scorer — offline only, guarded
│   │   │   ├── llm_client.py          # draft + extract — simulated or Anthropic
│   │   │   ├── ptp_extractor.py       # validates extraction before PromiseToPay
│   │   │   ├── ptp_followup.py        # due-row processor — manual / cron
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
│   ├── alembic/versions/     # 3 revisions — only way schema changes
│   ├── scripts/
│   │   ├── send_test_webhook.py       # signed webhook sender (local testing)
│   │   ├── run_synthetic_batch.py     # 100-case policy health check
│   │   ├── evaluate_policies.py       # baseline vs ML script (non-persisted)
│   │   └── process_followups.py       # manual BROKEN-promise processor
│   ├── tests/                # 233 hermetic tests
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

- Active branch for this pass: `feat/recoveryos-live-runtime`.
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
| `REDIS_URL` | No | `redis://localhost:6379/0` | No — reserved, no current import |
| `RAZORPAY_API_ENABLED` | No | `false` | **Gate** — live Payment Links only when `true` + valid key |
| `RAZORPAY_KEY_ID` | When enabled | `""` | Only with gate; `rzp_test_*` enforced |
| `RAZORPAY_KEY_SECRET` | When enabled | `""` | Only with gate |
| `RAZORPAY_WEBHOOK_SECRET` | Yes | `""` → **fails closed** (signature never passes) | No |
| `LLM_API_ENABLED` | No | `false` | **Gate** — Anthropic only when `true` |
| `LLM_PROVIDER` | No | `anthropic` | Non-`anthropic` raises `LLMAPIError` when enabled |
| `LLM_API_KEY` | When enabled | `""` | Only with gate |
| `FRONTEND_ORIGIN` | No | `http://localhost:5173` | CORS `allow_origins` |
| `VITE_API_BASE_URL` | No | `http://localhost:8000` | Frontend fetch base |

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
4. Decide if it needs execution: add to `action_executor.EXECUTABLE_ACTION_TYPES` and add an `_execute_<action>` branch in `execute` (follow `_execute_create_payment_link` / `_execute_contact` for audit(shape) — `AuditEvent(action_executed, {simulated, …})` and failure → `FAILED + HUMAN_REVIEW`).
5. Update ML ground truth if appropriate: `BASE_PROBABILITY[(category, action)]` in `app/ml/ground_truth.py:33`; out-of-table pairs fall back to `FALLBACK_PROBABILITY`, which may not be what you want.
6. Update `CONTACT_ACTIONS` if the action counts as contact/friction (`policy_engine.py:38` also drives `ptp_followup` and experiment counting).
7. Add tests: `test_policy_engine.py` param for `CATEGORY_TOP_CHOICE` (or guardrail block), `test_action_executor.py` for execute + failure, `test_experiments.py` or `test_ml_*` if scoring changes.

---

## How to add a new webhook event

1. In `app/services/orchestrator.py`, add to the appropriate set (`FAILURE_EVENTS`, `RECOVERY_EVENTS`, `DISPUTE_EVENTS`, or a new `PAYMENT_LINK_PAID_EVENT` analogue) and handle it in `handle_event` before the fallback `return None`.
2. Add the `raw_payload` extraction (use `_entity_from_payload` or a specific `_payment_link_entity_from_payload` analogue; keep `util: payload.payload.values()` iteration convention).
3. Decide state eligibility (`DIAGNOSABLE_STATES` / `REPLYABLE_STATES` analogues) — add a `TERMINAL_STATES` guard if appropriate. Current rules: recovery ignored from `RECOVERED/STOPPED/DISPUTED` (`orchestrator.py:206`); dispute always overrides (`orchestrator.py:248` — no terminal guard).
4. Add audit events; treat `PaymentEvent` as append-only.
5. Add `send_test_webhook.py` helper if manual testing is useful (see `payment_link.paid` payload in `tests/test_webhooks.py:181`).
6. Add `tests/test_webhooks.py` coverage including `400` on malformed body / `duplicate_event` idempotency.
7. Update `docs/SYSTEM_FLOWS.md` with the new flow and `ARCHITECTURE.md` Razorpay boundary.

---

## How to update state transitions

1. Change **only** `orchestrator.py` (case `state = …` mutations) or `policy_engine.ACTION_TO_STATE` + `action_executor.execute` for policy-owned/executor-owned steps. Do not scatter `case.state =` across routers or ML code.
2. Update `REPLYABLE_STATES` / `DIAGNOSABLE_STATES` / `TERMINAL_STATES` sets where transition eligibility changes.
3. Add/correct `AuditEvent(event, detail)` for every new mutation — these are the only way the dashboard/detail endpoint and inspectors explain "why".
4. Update `ARCHITECTURE.md` state machine diagram and table (`State inventory`, `Known deviation`), and `docs/SYSTEM_FLOWS.md` row for every affected flow.
5. Add migration if the transition needs a persisted column (e.g., a new `scheduled_for` semantic on `Action`).

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

API client: `src/api.ts` — typed `HealthResponse`, `RevenueCase`, `ArmSummary`, `ExperimentSummary`. `ExperimentPanel.tsx` drives `POST /experiments` + CSV export; `App.tsx` renders health + case list (no `GET /cases/{id}` detail view yet).

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

- [ ] Tests pass: `cd backend && python -m pytest -q` (233 pass, no external network).
- [ ] Frontend passes: `cd frontend && npm run lint && npm run build`.
- [ ] Migrations verify: `cd backend && DATABASE_URL=sqlite:///./ci_migration.db python -m alembic upgrade head`.
- [ ] No secrets introduced into diff (`backend/.env`, `frontend/.env` are gitignored — check `.env.example` only).
- [ ] Documentation updated per table above; `git diff --check` (whitespace) clean.
- [ ] Report lists docs changed / intentionally unchanged / reason.
