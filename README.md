# RecoveryOS

RecoveryOS is an adaptive revenue-recovery controller built for the Razorpay AI Buildathon, Track 03. It turns failed-payment events into auditable recovery decisions while balancing expected recovery value, operational cost, and customer friction.

Instead of sending the same reminder after every failure, RecoveryOS can wait for a native retry, create a payment link, draft customer outreach, collect a promise to pay, escalate for review, or stop. Deterministic guardrails remain authoritative throughout the workflow.

## Capabilities

- Verifies Razorpay webhook signatures and processes events idempotently.
- Classifies payment failures into eight deterministic categories.
- Applies contact limits, cooldowns, amount caps, and stopping rules before selecting an action.
- Supports baseline, shadow, and adaptive recovery policies.
- Ranks allowed actions using expected value, action cost, and customer-friction penalties.
- Executes durable delayed actions through PostgreSQL-backed state and Redis/RQ transport.
- Creates simulated or Razorpay Test Mode payment links and reconciles ambiguous provider outcomes.
- Extracts promise-to-pay details and drafts customer messages with deterministic validation and fallback.
- Provides a React operations console with case timelines, decision explanations, provider status, and experiment results.

## Scope And Safety

- Razorpay integration is restricted to Test Mode. Production keys are rejected.
- Customer messages are stored as drafts and are not sent through SMS, email, or WhatsApp.
- Training data and policy experiments are synthetic. Their results validate implementation behavior, not production lift.
- The LLM is optional and never selects recovery actions or establishes payment state.
- PostgreSQL is the source of truth; Redis/RQ is reconstructable execution transport.

## Architecture

```text
Razorpay webhooks ---> FastAPI API <--- React operations console
                          |
                          v
             diagnosis -> guardrails -> policy
                          |
                          v
              PostgreSQL authoritative state
                          |
                          v
                 scheduled actions
                          |
                          v
                   Redis/RQ worker
                          |
                          v
        execution, reconciliation, and follow-ups

FastAPI API -> synthetic experiment runner -> PostgreSQL
```

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for component boundaries, state transitions, and data flow diagrams.

## Quick Start

### Prerequisites

- Python 3.12
- Node.js 22
- Docker Desktop

### 1. Start PostgreSQL And Redis

```bash
docker compose up -d
```

### 2. Start The Backend

From `backend/`:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Start the worker in a second activated terminal from `backend/`:

```powershell
rq worker --worker-class app.worker.WindowsWorker --with-scheduler --url redis://localhost:6379/0 recoveryos
```

macOS and Linux commands are documented in [`docs/RUNBOOK.md`](./docs/RUNBOOK.md).

### 3. Start The Frontend

From `frontend/`:

```powershell
Copy-Item .env.example .env
npm install
npm run dev
```

Open `http://localhost:5173`.

## Local Demo

The demo runs without external credentials by using simulated payment links and deterministic language fallbacks.

From `backend/`:

```powershell
python -m app.ml.train
python scripts/seed_demo.py --reset-demo
python scripts/seed_demo.py --check
```

Open the frontend and use the seeded cases to inspect waiting, payment-link, promise-to-pay, adaptive-policy, and safety flows. A repeatable walkthrough is available in [`docs/DEMO_SCRIPT.md`](./docs/DEMO_SCRIPT.md).

## Verification

Backend tests are hermetic: they use a temporary SQLite database and deny external network access.

```powershell
cd backend
python -m pytest -q
```

```powershell
cd frontend
npm run lint
npm run build
```

## Technology

| Layer | Technology |
|-------|------------|
| Frontend | React, TypeScript, Vite, Tailwind CSS |
| Backend | FastAPI, Pydantic, SQLAlchemy, Alembic |
| Data | PostgreSQL; SQLite for tests |
| Jobs | Redis and RQ |
| ML | scikit-learn `HistGradientBoostingClassifier` |
| LLM | Optional Anthropic or OpenCode Zen; deterministic fallback by default |
| Payments | Signed Razorpay webhooks and Test Mode Payment Links |

## Documentation

| Document | Contents |
|----------|----------|
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Architecture, state model, and data boundaries |
| [`docs/CURRENT_STATE.md`](./docs/CURRENT_STATE.md) | Implemented capabilities and known limitations |
| [`docs/SYSTEM_FLOWS.md`](./docs/SYSTEM_FLOWS.md) | End-to-end workflow details |
| [`docs/RUNBOOK.md`](./docs/RUNBOOK.md) | Setup, operation, and troubleshooting |
| [`docs/ML_AND_EVALUATION.md`](./docs/ML_AND_EVALUATION.md) | Model training and synthetic evaluation methodology |
| [`docs/INTEGRATIONS.md`](./docs/INTEGRATIONS.md) | Razorpay, LLM, and Redis/RQ integration boundaries |
| [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) | Development conventions and extension guides |
| [`docs/DECISIONS.md`](./docs/DECISIONS.md) | Architecture decision records |
