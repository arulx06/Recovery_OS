# RecoveryOS — Adaptive Revenue Recovery Controller

Built for the **Razorpay AI Buildathon**, Track 03 (AI Revenue Recovery).

> When revenue fails, RecoveryOS decides whether to wait for a native retry, contact the customer, send a payment link, collect a promise-to-pay, escalate, or stop — and measures which decisions actually recover the most money with the least customer friction.

This is intentionally **not** `failure → LLM → WhatsApp message → payment link`. Razorpay already ships subscription retries and recovery agents that do variations of that. RecoveryOS answers a narrower, harder question: *given that revenue is at risk, what is the single best next intervention — including doing nothing?*

---

## What judges should know in 2 minutes

- **Problem:** Payment failures beyond gateway routing — every failed payment needs a recovery decision, but most stacks treat all failures the same.
- **What RecoveryOS adds** (vs. Razorpay today): deterministic failure taxonomy → guardrailed policy → measured baseline-vs-adaptive experiments with downloadable audits. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full boundary table.
- **Current scope:** Signed webhook ingestion, deterministic diagnosis, guardrails + baseline policy, simulated and Test-Mode Payment Links, stored (not delivered) customer messages, promise-to-pay extraction, offline adaptive ML, and a measurement dashboard — all on a modular-monolith with PostgreSQL as the source of truth.
- **What is not there yet:** No automatic scheduler/worker for parked states, no delivered SMS/email/WhatsApp, no production live-money integration, no LLM controlling money moves.

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
| Payment Links — Razorpay **Test Mode** live call | ✅ Verified (manual) |
| `payment.captured` / `payment_link.paid` → `RECOVERED` | ✅ Verified |
| Promise-to-Pay extraction (regex heuristic / optional Anthropic) | ✅ Simulated default |
| Adaptive ML scorer (`HistGradientBoostingClassifier`, expected-value ranking) | ✅ Offline only — not live |
| Persisted experiments (baseline vs adaptive, `run_id` + CSV audit) | ✅ Verified |
| React dashboard + health + case list | ✅ Verified |
| Hermetic test suite (233 tests; no external network) | ✅ Verified |

**Important limits:** Delayed actions are persisted rows only — `WAIT`/`FOLLOW_UP_PTP` have **no automatic wake-up** (`docs/CURRENT_STATE.md` → Runtime reality). Drafted customer messages are **stored, not delivered**. The adaptive policy is **offline/synthetic** and does not serve the live webhook path. Redis/RQ are reserved dependencies with no worker. See the full matrix at [`docs/CURRENT_STATE.md`](./docs/CURRENT_STATE.md).

---

## Architecture at a glance

```
Razorpay Test Mode  ──webhooks──►  FastAPI  ──►  Failure diagnosis ──►  Guardrails ──►  Baseline policy
                                    │                     │                  │                    │
                                    │                     ▼                  ▼                    ▼
                                    │              PostgreSQL (source of truth: cases, events, decisions, actions, audit)
                                    │                     ▲
                                    └────  Action executor (Payment Links — simulated or Test Mode; contact drafts — stored only)
                                    └────  Customer reply / PTP follow-up (manual-only scheduling)
                                    └────  Offline adaptive ML + experiment runner (synthetic)
                                    └────  React dashboard
```

- Deterministic code decides when money moves; LLM never does — it drafts text or parses a reply.
- PostgreSQL is authoritative; Redis/RQ are future transport (planned, not wired).
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
| Delayed actions | DB records + manual processor; Redis/RQ reserved |
| ML | scikit-learn `HistGradientBoostingClassifier` (offline/synthetic) |
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

- Branch for this pass: `feat/recoveryos-live-runtime` (see `docs/CURRENT_STATE.md` for verified baseline).
- Backend tests: `cd backend && python -m pytest` — uses a temp SQLite DB and denies external sockets.
- Frontend: `cd frontend && npm run lint && npm run build`.

Detailed validation, state-machine, API, and configuration references are in `ARCHITECTURE.md` and `docs/*`.
