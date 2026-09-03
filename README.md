# RecoveryOS — Adaptive Revenue Recovery Controller

Built for the **Razorpay AI Buildathon**, Track 03 (AI Revenue Recovery).

> When revenue fails, RecoveryOS decides whether to wait for a native retry, contact the customer, send a payment link, collect a promise-to-pay, escalate, or stop — and measures which decisions actually recover the most money with the least customer friction.

This is intentionally **not** `failure → LLM → WhatsApp message → payment link`. Razorpay already ships subscription retries and recovery agents that do variations of that. RecoveryOS answers a narrower, harder question: *given that revenue is at risk, what is the single best next intervention — including doing nothing?*

---

## What judges should know in 2 minutes

- **Problem:** Payment failures beyond gateway routing — every failed payment needs a recovery decision, but most stacks treat all failures the same.
- **What RecoveryOS adds** (vs. Razorpay today): deterministic failure taxonomy → guardrailed policy → measured baseline-vs-adaptive experiments with downloadable audits. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full boundary table.
- **Current scope:** Signed webhook ingestion, deterministic diagnosis, guardrails + baseline policy, **live friction-aware adaptive policy** (`RECOVERY_POLICY=baseline|shadow|adaptive`, `recovery-v1` `75e9cfd6`, `balanced` weight 18, manifest + fingerprint), PostgreSQL-authoritative Redis/RQ temporal execution, provider-reconciled Payment Links, linked promise-to-pay deadlines, optional LLM-assisted customer-language intelligence (structured PTP extraction + safe message drafting with deterministic fallback), and a **merchant observability console** — failure diagnosis, guardrail, friction, Decision Inspector, chronological timeline, provider truth, and synthetic experiment tradeoff. RecoveryOS uses guarded friction-aware adaptive ML for recovery decisions and an optional LLM only for customer-language tasks such as Promise-to-Pay extraction and communication drafting.
- **What is not there yet:** No delivered SMS/email/WhatsApp, no production live-money integration, and no LLM controlling money moves. Provider exactly-once is bounded by `reference_id` uniqueness + reconciliation; synthetic training remains the only labeled data.

**Two disclaimers (read before evaluating numbers):**

- **Razorpay TEST MODE only.** The only live Razorpay path verified is `POST /v1/payment_links` with a `rzp_test_*` key producing a genuine `plink_*` and `rzp.io` URL. `TEST MODE ≠ production`. See [`docs/INTEGRATIONS.md`](./docs/INTEGRATIONS.md).
- **Synthetic evaluation only.** Training data, model metrics, and baseline-vs-adaptive comparisons are generated from the same hand-authored simulator (`app/ml/ground_truth.py`). They validate code and assumptions, not production lift. See [`docs/ML_AND_EVALUATION.md`](./docs/ML_AND_EVALUATION.md).

---

## Verified capabilities (today)

| Capability | Status |
|------------|--------|
| Webhook HMAC-SHA256 verification + idempotent ingestion | ✅ Verified |
| 8-category deterministic failure diagnosis | ✅ Verified |
| Guardrailed baseline policy (contact limits, cooldown, amount cap, stopping rule) | ✅ Verified |
| Payment Links — simulated (`plink_sim_*`) | ✅ Verified |
| Payment Links — Razorpay **Test Mode** live call (`reference_id=Action.id`) | ✅ Verified (manual) |
| Payment Link provider reconciliation (`GET ?reference_id=` + validated adopt) | ✅ Verified — ambiguous/stale `EXECUTING` reconciles before retry; mismatch → `HUMAN_REVIEW` |
| `payment.captured` / `payment_link.paid` → `RECOVERED` | ✅ Verified (idempotent) |
| Promise-to-Pay extraction (structured, deterministic validation, optional Anthropic) | ✅ Verified — `LLM_API_ENABLED=false` regex fallback, deterministic validation, merchant-local relative dates, injection defense |
| LLM message drafting (safe `[[PAYMENT_LINK]]` placeholder, DRAFT only) | ✅ Verified — `LLM_API_ENABLED=false` templated fallback, provider-authoritative URL substitution, no invented amount/discount |
| LLM deterministic fallback & provenance | ✅ Verified — typed `LLMUnavailableError`/`LLMInvalidResponseError`, prompt versions `ptp-v1`/`message-v1`, stored `generation_method/extraction_method/provider/model/prompt_version/amount_method` |
| Durable `WAIT` / native-retry / linked PTP scheduling via Redis/RQ | ✅ Verified on PostgreSQL + Redis |
| Atomic action claims, bounded retries, stale-job no-ops, DB reconciliation | ✅ Verified (now with Payment Link ambiguity) |
| Adaptive ML scorer (`HistGradientBoostingClassifier`, friction-aware `P*amount - cost - weight*friction`, `recovery-v1`) | ✅ **Live** — `baseline` (default, safe), `shadow` (audited), `adaptive` (balanced, fallback to baseline) |
| Friction-aware experiments (baseline vs balanced adaptive, `run_id` + CSV + `friction_score`/`utility`) | ✅ Verified (synthetic) — now with revenue-vs-friction Pareto & action distribution |
| React merchant console: dashboard, filtered case list, Decision Inspector, timeline, provider truth, PTP & health | ✅ Verified — control-center UX: failure → guardrails → friction-aware candidates → temporal → reconciliation → PTP → recovered |
| Hermetic test suite (434 tests; no external network or Redis required) | ✅ Verified — includes dashboard, case filters, explainability, timeline, demo seed |

**Important limits:** The worker and periodic reconciliation are required operational processes. Drafted customer messages are **stored, not delivered**. Training/evaluation remain **synthetic** (no production Razorpay lift claim); adaptive is live but friction weights are policy preferences, not measured costs. See the full matrix at [`docs/CURRENT_STATE.md`](./docs/CURRENT_STATE.md).

---

## Architecture at a glance

```
Razorpay Test Mode  ──webhooks──►  FastAPI  ──►  Failure diagnosis ──►  Guardrails ──►  Policy Dispatcher (baseline|shadow|adaptive)
                                    │                     │                  │                    │
                                    │                     └─────────┬────────┘                    │
                                    │                               ▼                             ▼
                                    │              PostgreSQL (cases, events, decisions+provenance, actions, audit)
                                    │                     ▲
                                    └────  Redis/RQ worker (ID-only jobs; atomic DB claims; provider-reconciled Links)
                                    └────  Action executor (Payment Links — simulated or Test-Mode; contact drafts — stored only)
                                    └────  Customer reply / linked PTP follow-up
                                    └────  Experiment runner (baseline vs friction-aware adaptive, common-random)
                                    └────  React merchant console (read-model → explainability → timeline → provider truth)
```

- Deterministic code decides when money moves; LLM never does — it drafts text or parses a reply.
- PostgreSQL is authoritative; Redis/RQ is reconstructable execution transport.
- See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for component boundaries, state machine, and Mermaid diagrams.

---

## Quick start

Detailed, Windows-friendly steps with PowerShell vs CMD splits are at **[`docs/RUNBOOK.md`](./docs/RUNBOOK.md)**. Summary:

**1. Start Postgres + Redis**

```bash
docker compose up -d
```

**2. Backend**

```bash
cd backend
# Windows PowerShell:
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
# macOS/Linux variant is in RUNBOOK.md
```

In a second activated Windows terminal from `backend/`:

```bash
rq worker --worker-class app.worker.WindowsWorker --with-scheduler --url redis://localhost:6379/0 recoveryos
```

Health check: `curl http://localhost:8000/health`

**3. Frontend**

```bash
cd frontend
# Windows PowerShell: Copy-Item .env.example .env
# macOS/Linux: cp .env.example .env
npm install
npm run dev
```

Open `http://localhost:5173` — header should show `backend healthy`.

**Try it without credentials** (uses simulation):

```bash
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_1 --amount 4999 --error-reason card_expired
python scripts/send_test_webhook.py payment.captured --payment-id pay_demo_1 --amount 4999
curl http://localhost:8000/cases
```

See [`docs/RUNBOOK.md`](./docs/RUNBOOK.md) for Payment Link → `payment_link.paid` loop, PTP reply flow, training, and experiment commands.

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Frontend | React + Vite + TypeScript, Tailwind CSS |
| Backend | Python + FastAPI, Pydantic, SQLAlchemy + Alembic |
| Database | PostgreSQL (SQLite for tests) |
| Delayed actions | PostgreSQL-authoritative `Action` rows + Redis/RQ worker/scheduler |
| ML | scikit-learn `HistGradientBoostingClassifier` (`recovery-v1`, 75e9cfd6, `features v1`, friction-aware utility, `manifest.json`) |
| LLM | Optional Anthropic Messages API; templated/regex simulation otherwise |
| Payments | Signed webhooks; simulated or opt-in Test-Mode Payment Links |

---

## Documentation map

| Document | Canonical for |
|----------|---------------|
| [`docs/CURRENT_STATE.md`](./docs/CURRENT_STATE.md) | What is implemented / partial / planned **right now** — the first file future phases must update |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | System design, component boundaries, state machine, data model |
| [`docs/SYSTEM_FLOWS.md`](./docs/SYSTEM_FLOWS.md) | Step-by-step lifecycle for each major flow |
| [`docs/RUNBOOK.md`](./docs/RUNBOOK.md) | Local operator runbook (Windows-friendly) — every command that works |
| [`docs/ML_AND_EVALUATION.md`](./docs/ML_AND_EVALUATION.md) | Training data, simulator, metrics, circularity, revenue-vs-friction tradeoff |
| [`docs/INTEGRATIONS.md`](./docs/INTEGRATIONS.md) | Razorpay, LLM, Redis/RQ boundaries — real vs simulated vs Test Mode |
| [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) | Repo structure, adding a feature, definition of done, doc governance |
| [`docs/DECISIONS.md`](./docs/DECISIONS.md) | Architectural Decision Records |

Future subsystems are not considered complete until their documentation still describing the old behavior has been updated.

---

## Repository

- Branch for this pass: `feat/observability-demo-ux` (see `docs/CURRENT_STATE.md` for verified baseline).
- Backend tests: `cd backend && python -m pytest` — 434 tests, temp SQLite DB, no external sockets, no Redis.
- Frontend: `cd frontend && npm run lint && npm run build` — control-center console: Overview / Cases / Experiments / System with Decision Inspector, timeline, and provider truth. DRAFT / NOT SENT banner preserved.
- Demo: `cd backend && python scripts/seed_demo.py` then open `http://localhost:5173/?case=<id>`

Detailed validation, state-machine, API, and configuration references are in `ARCHITECTURE.md` and `docs/*`.
