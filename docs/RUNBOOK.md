# RecoveryOS — Runbook

> Windows-friendly. Every block states its shell and working directory. Do not mix PowerShell `$env:` syntax with CMD `set` syntax.

---

## Prerequisites

| Requirement | Version tested | Check |
|-------------|---------------|-------|
| Python | 3.12 | `python --version` |
| Node.js | 22 | `node --version` |
| Docker Desktop | any recent | `docker --version` |
| Git | any recent | `git --version` |

This runbook assumes the repository root is `D:\Razorpay` (or wherever you cloned to) and that you are on branch `feat/recoveryos-live-runtime` (`git branch --show-current`).

---

## 1. Start Postgres + Redis

These are the only services run under Docker locally; the backend and frontend run with `uvicorn` / `npm run dev` directly.

**PowerShell / CMD (same):**

```bash
docker compose up -d
```

Verify:

```bash
docker compose ps
# postgres 5432 up, redis 6379 up
```

Stop later:

```bash
docker compose down        # keep data
docker compose down -v     # also wipe recoveryos_pg_data volume
```

---

## 2. Backend — install and start

### 2a. Environment file

**PowerShell:**

```powershell
Copy-Item backend\.env.example backend\.env
# then edit backend\.env to set RAZORPAY_WEBHOOK_SECRET (any non-empty string
# is enough for the simulated path); leave RAZORPAY_API_ENABLED=false unless
# you are doing Razorpay Test Mode verification.
```

**CMD:**

```cmd
copy backend\.env.example backend\.env
```

**macOS / Linux:**

```bash
cp backend/.env.example backend/.env
```

Do not put real Razorpay secret/key values in docs or screenshots.

### 2b. Virtual environment and dependencies

Working directory for all `pip`/`alembic`/`uvicorn`/`pytest`/`python -m app…` commands is **`backend/`** — not the repository root.

**PowerShell:**

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

**CMD:**

```cmd
cd backend
python -m venv .venv
.\.venv\Scripts\activate.bat
pip install -r requirements.txt
```

**macOS / Linux:**

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2c. Migrate

Migrations are owned by Alembic. Run them before starting the app, and after every `git pull` that changes `backend/alembic/versions/*`.

Revision `8d6e24f91a73` adds a live-Razorpay payment-ID unique index. It checks for legacy duplicate `source=razorpay` payment IDs before any schema change and aborts with a clear error if operator resolution is required. Subscription IDs are intentionally not unique because billing cycles reuse them.

```bash
alembic upgrade head
```

SQLite shortcut used by CI (not needed for local Postgres):

```bash
DATABASE_URL=sqlite:///./local.db alembic upgrade head
```

If this fails, confirm `DATABASE_URL` in `backend/.env` points to `postgresql+psycopg2://recoveryos:recoveryos@localhost:5432/recoveryos` and that `docker compose up -d` is running.

### 2d. Start

```bash
uvicorn app.main:app --reload --port 8000
```

### 2e. Start the RQ worker and scheduler

Run from `backend/` in a second activated terminal. On Windows, stock RQ 1.16.2 uses unavailable `fork`/`SIGALRM` primitives, so the custom worker class is required.

**PowerShell / CMD:**

```bash
rq worker --worker-class app.worker.WindowsWorker --with-scheduler --url redis://localhost:6379/0 recoveryos
```

**macOS / Linux:**

```bash
rq worker --with-scheduler --url redis://localhost:6379/0 recoveryos
```

The worker must run from `backend/` so `app.jobs.process_action` imports and `backend/.env` loads. `--with-scheduler` is required for future `WAIT` and PTP jobs.

Run reconciliation after startup and periodically (for example, once per minute via Task Scheduler/cron):

```bash
python scripts/reconcile_actions.py
```

This repairs expired `EXECUTING` claims and republishes all PostgreSQL `SCHEDULED` actions. It is safe to repeat. Review the count before starting a worker against an existing database because historical scheduled actions may become executable.

Expected: `Uvicorn running on http://127.0.0.1:8000`.

### 2e. Health check

**PowerShell / CMD / macOS / Linux:**

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"recoveryos-backend","database":"connected"}
```

If `database` is `unreachable`, check `docker compose ps` and that `backend/.env: ENV=dev` (not `test`).

**Root endpoint:**

```bash
curl http://localhost:8000/
# {"service":"RecoveryOS","phase":"7 - measurement + explainability dashboard"}
```

---

## 3. Frontend — install and start

Working directory for `npm` commands is **`frontend/`**.

**PowerShell / CMD / macOS / Linux:**

```bash
cd frontend
```

Then environment file:

**PowerShell:**

```powershell
Copy-Item .env.example .env
```

**CMD:**

```cmd
copy .env.example .env
```

**macOS / Linux:**

```bash
cp .env.example .env
```

Default `VITE_API_BASE_URL` in `.env.example` is `http://localhost:8000` — keep it.

Install and start:

```bash
npm install
npm run dev
```

Open `http://localhost:5173` — header should show `backend healthy` and database row `connected`.

Lint / build (required for CI parity):

```bash
npm run lint
npm run build
```

---

## 4. Tests (hermetic — never hit `.env` credentials or external network)

Working directory: **`backend/`** with the venv activated.

```bash
python -m pytest -q
# 257 passed
```

What "hermetic" means here (`tests/conftest.py`):

- `ENV=test`, `DATABASE_URL=sqlite:///<tempfile>/test.db` (process-unique, removed after session).
- `RAZORPAY_API_ENABLED=false`, `LLM_API_ENABLED=false` regardless of `backend/.env`.
- `TASK_QUEUE_ENABLED=false` and an inert loopback `REDIS_URL`; Redis is not required.
- Fake `RAZORPAY_KEY_ID`/`LLM_API_KEY` values so missing-credential branches are exercised but never make network calls.
- `socket.connect` / `socket.connect_ex` / `socket.sendto` guarded to loopback only; any external-network attempt raises.

Collect-only (no run):

```bash
python -m pytest --collect-only -q
```

CI uses `DATABASE_URL=sqlite:///./ci.db python -m pytest -q` — same constraints.

---

## 5. Simulated webhook testing (no Razorpay credentials)

Set `RAZORPAY_WEBHOOK_SECRET` in `backend/.env` to any non-empty string, restart the backend, then export the same string so `send_test_webhook.py` signs with it.

**PowerShell:**

```powershell
$env:RAZORPAY_WEBHOOK_SECRET="change_me_local_secret"
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_1 --amount 4999 --error-reason card_expired
python scripts/send_test_webhook.py payment.captured --payment-id pay_demo_1 --amount 4999
curl http://localhost:8000/cases
# the pay_demo_1 case should show state RECOVERED
```

**CMD:**

```cmd
set RAZORPAY_WEBHOOK_SECRET=change_me_local_secret
python scripts\send_test_webhook.py payment.failed --payment-id pay_demo_1 --amount 4999 --error-reason card_expired
python scripts\send_test_webhook.py payment.captured --payment-id pay_demo_1 --amount 4999
curl http://localhost:8000/cases
```

**macOS / Linux:**

```bash
export RAZORPAY_WEBHOOK_SECRET=change_me_local_secret
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_1 --amount 4999 --error-reason card_expired
python scripts/send_test_webhook.py payment.captured --payment-id pay_demo_1 --amount 4999
curl http://localhost:8000/cases
```

**Note:** Working directory for `scripts/send_test_webhook.py` is `backend/` **or** `backend/scripts/` — the script uses `DEFAULT_URL=http://localhost:8000/webhooks/razorpay`. Its `--event-id` defaults to a fresh `evt_local_<ms>`; pass `--event-id evt_demo_dup` twice with the same value to verify duplicate-event handling returns `{"status":"ignored","reason":"duplicate_event"}`.

---

## 6. Razorpay Test Mode testing (real API call)

Gates — all three must hold, or the code falls back to simulation:

1. `backend/.env`: `RAZORPAY_API_ENABLED=true`
2. `RAZORPAY_KEY_ID` starts with `rzp_test_`
3. `RAZORPAY_KEY_SECRET` non-empty

`rzp_live_*` keys raise `RazorpayAPIError: RecoveryOS only permits Razorpay Test Mode API keys` (`app/services/razorpay_client.py:72`). Credentials alone without `RAZORPAY_API_ENABLED=true` never trigger network.

Steps:

1. Obtain `rzp_test_*` credentials from the Razorpay Dashboard (Test Mode) and put them in `backend/.env` (do not commit the file).
2. `uvicorn ... --reload` picks them up on restart (or `load_dotenv` on next process start).
3. Run the same `send_test_webhook.py` flow with `error-reason card_expired` (maps to `INVALID_INSTRUMENT` → `CREATE_PAYMENT_LINK`):

**PowerShell:**

```powershell
$env:RAZORPAY_WEBHOOK_SECRET="<same as backend/.env>"
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_2 --amount 1800 --error-reason card_expired
```

Then inspect:

```bash
curl http://localhost:8000/cases | python -m json.tool
curl http://localhost:8000/cases/<case-id> | python -m json.tool
# details.actions[0].result should contain a genuine plink_* id,
# details.actions[0].result.simulated == false,
# and detail '{"short_url": "https://rzp.io/..."}' (not /simulated/).
```

4. To drive the recovery side, continue with the **Payment Link loop** (works with both simulated and genuine links):

**PowerShell** (note variable syntax differs from bash):

```powershell
$cases = Invoke-RestMethod http://localhost:8000/cases
$linkId = $cases[0].razorpay_payment_link_id
python scripts/send_test_webhook.py payment_link.paid --payment-link-id $linkId --amount 1800
curl http://localhost:8000/cases
# case should show state RECOVERED, audit event case_recovered_silently
```

**CMD (no `$(...)`):**

```cmd
REM fetch the link id manually from the curl output:
curl http://localhost:8000/cases
python scripts\send_test_webhook.py payment_link.paid --payment-link-id plink_XXXXXXXX --amount 1800
curl http://localhost:8000/cases
```

**macOS / Linux:**

```bash
LINK_ID=$(curl -s http://localhost:8000/cases | python3 -c "import json,sys;print(json.load(sys.stdin)[0]['razorpay_payment_link_id'])")
python scripts/send_test_webhook.py payment_link.paid --payment-link-id "$LINK_ID" --amount 1800
curl http://localhost:8000/cases
```

Do not paste key/secret values into chat, docs, or telemetry.

---

## 7. Promise-to-Pay test flow

Requires a case that the baseline routes to `CONTACT_CUSTOMER` (auth flows work well — `otp_incorrect`, `authentication_failed`, etc.).

**PowerShell:**

```powershell
$env:RAZORPAY_WEBHOOK_SECRET="<same as backend/.env>"
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_3 --amount 8000 --error-reason otp_incorrect

# retrieve case id:
$cases = Invoke-RestMethod http://localhost:8000/cases
$caseId = $cases[0].id
Invoke-RestMethod http://localhost:8000/cases/$caseId | ConvertTo-Json -Depth 10
# observe the outbound message draft at .messages[0].body

# customer replies "I'll pay 8000 Friday"
Invoke-RestMethod -Method Post -Uri http://localhost:8000/cases/$caseId/customer-reply `
  -ContentType "application/json" `
  -Body '{"body": "I'\''ll pay 8000 Friday"}'
# -> {"case_state":"AWAITING_OUTCOME"} with a PromiseToPay + scheduled FOLLOW_UP_PTP

# customer disputes
Invoke-RestMethod -Method Post -Uri http://localhost:8000/cases/$caseId/customer-reply `
  -ContentType "application/json" `
  -Body '{"body": "actually this is wrong, I already paid this"}'
# -> {"case_state":"DISPUTED"} — even overriding a pending promise
```

**CMD:**

```cmd
set RAZORPAY_WEBHOOK_SECRET=<same as backend/.env>
python scripts\send_test_webhook.py payment.failed --payment-id pay_demo_3 --amount 8000 --error-reason otp_incorrect
curl http://localhost:8000/cases

curl -X POST http://localhost:8000/cases/<CASE_ID>/customer-reply ^
  -H "Content-Type: application/json" -d "{\"body\": \"I'll pay 8000 Friday\"}"

curl -X POST http://localhost:8000/cases/<CASE_ID>/customer-reply ^
  -H "Content-Type: application/json" -d "{\"body\": \"actually this is wrong, I already paid this\"}"
```

**macOS / Linux:**

```bash
export RAZORPAY_WEBHOOK_SECRET=<same as backend/.env>
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_3 --amount 8000 --error-reason otp_incorrect
CASE_ID=$(curl -s http://localhost:8000/cases | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
curl -s http://localhost:8000/cases/$CASE_ID | python3 -m json.tool
curl -X POST http://localhost:8000/cases/$CASE_ID/customer-reply -H "Content-Type: application/json" -d '{"body": "I'\''ll pay 8000 Friday"}'
curl -X POST http://localhost:8000/cases/$CASE_ID/customer-reply -H "Content-Type: application/json" -d '{"body": "actually this is wrong, I already paid this"}'
```

Message drafting / intent extraction are **simulated** by default (`channel=simulated`); to use Anthropic, set `LLM_PROVIDER=anthropic`, `LLM_API_KEY`, `LLM_API_ENABLED=true` in `backend/.env`.

The RQ scheduler processes the linked promise after the full merchant-local promised day. To repair missed queue publication or abandoned claims:

```bash
# working directory: backend/
python scripts/reconcile_actions.py
# process_followups.py remains a compatibility alias for the same reconciliation
```

For a short local wait test, stop/restart the backend and worker with `WAIT_DELAY_SECONDS=5`, send a transient failure, and watch the worker move the case out of `WAITING`. Do not use shortened delays in shared environments.

---

## 8. Model training

Working directory: **`backend/`**

```bash
# default: n=30000 synthetic rows, seed=42, artifact: app/ml/artifacts/model.joblib
python -m app.ml.train

# options:
python -m app.ml.train --n 6000 --seed 7
```

Produces: `n_train=24000`, `n_test=6000` rows (80/20 stratified split), prints `ROC-AUC`, `Log loss`, `Accuracy @0.5`, and `base_rate` (see `docs/ML_AND_EVALUATION.md` for the caveats and current approximate numbers).

The artifact directory is gitignored (`backend/.gitignore:8`). A fresh clone requires this step before any `POST /experiments` or `scripts/evaluate_policies.py` call — otherwise they return `503 ModelNotTrainedError`.

---

## 9. Synthetic evaluation

Working directory: **`backend/`**

### API (persisted, dashboard-visible)

```bash
curl -X POST http://localhost:8000/experiments -H "Content-Type: application/json" -d "{\"count\": 500, \"seed\": 11}"
curl http://localhost:8000/experiments/<run_id>
curl http://localhost:8000/experiments/<run_id>/export.csv -o audit.csv
curl http://localhost:8000/experiments   # recent run_id list
```

Or use the dashboard's **Run experiment** panel (`http://localhost:5173`).

### Script (non-persisted, prints table)

```bash
python scripts/evaluate_policies.py --count 1000 --seed 11
```

Both use **common random numbers** — when baseline and adaptive pick the same action for a scenario, they get the identical Bernoulli outcome (see `docs/ML_AND_EVALUATION.md`).

### Batch sanity check (Policy/Diagnosis only, no ML)

```bash
python scripts/run_synthetic_batch.py --count 100 --seed 7
# prints failure-category mix, chosen-action mix, and confirms no case is stuck in DETECTED/DIAGNOSED
```

---

## 10. Experiment endpoint (data contract)

See `docs/SYSTEM_FLOWS.md` flow 8 and `app/routers/experiments.py`:

- `POST /experiments` body: `{"count": 1..5000, "seed": int?}` → `ExperimentSummary` with `run_id`, `arms.baseline|adaptive`, `incremental_recovered`.
- CSV header: `run_id,arm,revenue_case_id,failure_category,chosen_action,amount_at_risk,amount_recovered,recovered,contacts_made,created_at` — one row per `(scenario, arm)`.

---

## 11. Working-directory rules

| Command | Must run from |
|---------|--------------|
| `python -m app.ml.train` | `backend/` |
| `python scripts/send_test_webhook.py …` | `backend/` (or absolute `backend/scripts/send_test_webhook.py`) |
| `python scripts/evaluate_policies.py …` | `backend/` |
| `python scripts/process_followups.py` | `backend/` |
| `python scripts/reconcile_actions.py` | `backend/` |
| `rq worker ... recoveryos` | `backend/` |
| `python scripts/run_synthetic_batch.py …` | `backend/` (it sets `DATABASE_URL=sqlite:///:memory:` itself) |
| `python -m pytest` | `backend/` |
| `alembic upgrade head` | `backend/` |
| `npm run dev / build / lint` | `frontend/` |
| `docker compose up -d` | repository root (where `docker-compose.yml` lives) |

Running `python -m app.ml.train` from the **repository root** raises `ModuleNotFoundError: No module named 'app'` — the working directory matters.

---

## 12. Shutdown / restart

```bash
# stop backend: Ctrl+C in the uvicorn terminal
# stop frontend: Ctrl+C in the vite terminal
docker compose down      # services stopped, data kept
# or
docker compose down -v   # also wipes Postgres volume — use after a known-bad migration attempt
```

After a restart: start Compose, run `alembic upgrade head`, run `python scripts/reconcile_actions.py`, then start uvicorn, the RQ worker with scheduler, and the frontend.

---

## 13. Troubleshooting (only issues still relevant as of this branch)

| Symptom | Fix |
|---------|-----|
| `uvicorn` says `could not connect to postgres` | `docker compose ps` must show `postgres 5432 up`; check `backend/.env:DATABASE_URL` (dev uses `postgresql+psycopg2://recoveryos:recoveryos@localhost:5432/recoveryos`, tests use `sqlite`). |
| `POST /webhooks/razorpay` returns `400 invalid webhook signature` | The raw-body HMAC must match `backend/.env:RAZORPAY_WEBHOOK_SECRET` and `send_test_webhook.py --secret` / `$env:RAZORPAY_WEBHOOK_SECRET`. Mismatch is intentional (fails closed). For tests, `conftest.py` uses `test_webhook_secret`. |
| `POST /webhooks/razorpay` returns `400 missing x-razorpay-event-id` | `send_test_webhook.py` generates one; a raw `curl` needs `-H "x-razorpay-event-id: evt_local_1"`. |
| `python -m app.ml.train` warns `No trained model at …` on experiments | `POST /experiments` without a model returns `503 ModelNotTrainedError` — run `python -m app.ml.train` first. Artifact is gitignored. |
| `/health` reports Redis `unreachable` | Confirm the Redis container is up and `REDIS_URL` is correct. Use `TASK_QUEUE_ENABLED=false` only for intentionally queue-free test processes. |
| Actions remain `SCHEDULED` | Start the worker with `--with-scheduler`, then run `python scripts/reconcile_actions.py`. Check `rq info --url redis://localhost:6379/0 recoveryos`. |
| RQ fails with `os.fork` or `signal.SIGALRM` on Windows | Use `--worker-class app.worker.WindowsWorker`; `SimpleWorker` alone still uses the Unix timeout class. |
| Actions remain `EXECUTING` after a worker crash | After `ACTION_CLAIM_TIMEOUT_SECONDS`, run reconciliation. It resets attempts still within budget and fails exhausted work to human review. |
| `npm run lint` fails | `oxlint` (not eslint) — run `npm install` first; check Node 22+. |
| `alembic upgrade head` says `already at head` | Fine - current head is `8d6e24f91a73`. CI verifies migrations against SQLite and local verification uses PostgreSQL. |

No real credentials are included above. For full boundaries see `docs/INTEGRATIONS.md`.
