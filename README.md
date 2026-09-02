# RecoveryOS — Adaptive Revenue Recovery Controller

Built for the **Razorpay AI Buildathon**, Track 03 (AI Revenue Recovery).

> When revenue fails, RecoveryOS decides whether to wait for a native retry, contact the
> customer, send a payment link, collect a promise-to-pay, escalate, or
> stop — and measures which decisions actually recover the most money with
> the least customer friction.

This is intentionally **not** `failure → LLM → WhatsApp message → payment link`. Razorpay already ships subscription retries and recovery agents that
do variations of that. RecoveryOS answers a narrower, harder question:
*given that revenue is at risk, what is the single best next intervention —
including doing nothing?* See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for
the full pipeline, state machine, and a table of what Razorpay already
provides versus what this project adds on top.

## Current implementation status

The modular-monolith prototype includes a signed webhook path, deterministic
failure diagnosis and baseline policy, simulated actions, opt-in Razorpay Test
Mode Payment Link creation, an offline adaptive ML policy, Promise-to-Pay
extraction, persisted synthetic experiments, and a measurement dashboard.
Adaptive ML is not used by the webhook path. Customer messages are drafted and
stored but are not delivered through SMS, email, or WhatsApp. Delayed actions
are records only; no worker or scheduler currently consumes them automatically.

- [X] `app/services/experiment_runner.py` — the persisted, downloadable
  version of Phase 5's `evaluate_policies.py`: runs matched synthetic
  scenarios through both policies (same common-random-numbers
  methodology), and records one `ExperimentCase` row per (scenario,
  arm) grouped by `run_id`
- [X] New migration: `run_id`, `failure_category`, `chosen_action` added
  to `experiment_cases` — needed to group a run and show its action
  distribution
- [X] `POST /experiments` — the one-click run (up to 5,000 cases per
  call); `GET /experiments` lists recent runs; `GET /experiments/{id}`
  returns the summary; `GET /experiments/{id}/export.csv` streams the
  full case-level audit (every synthetic case, both arms, the action
  each policy chose, the simulated outcome) as a downloadable CSV
- [X] Dashboard: a "Run experiment" panel with case-count and seed
  inputs, side-by-side baseline vs. adaptive cards (₹ at risk, ₹
  recovered, recovery rate, contacts, escalations, action
  distribution), an incremental-₹ headline, and a "Download audit
  CSV" link
- [X] `GET /cases/{id}` serves decision details and an audit timeline. The
  current frontend does not render this case-detail view.
- [X] Hermetic backend test suite, including explicit fake-credential and
  network-denial coverage

**Phase 7 measurement scope:** one click runs a 500+ scenario experiment with
metrics reproducible from a fixed seed and model, plus a downloadable audit.
Run IDs and creation timestamps differ between runs, so complete API responses
are not byte-identical. Reproducibility is scoped to *a fixed trained model* —
`HistGradientBoostingClassifier` can differ by a small fraction of a
percent between two separate *training* runs even with the same
`random_state`, a known consequence of its parallel histogram-building
reduction order. Re-running an experiment against the same already-trained
`model.joblib` is exactly reproducible; retraining first and then
comparing to an old run is not, and isn't expected to be.

Reinforcement learning, a live scheduler, and wiring the ML policy into
the production webhook path are all still out of scope — this dashboard
measures the adaptive policy, it doesn't make it the one that's live.

## Tech stack

| Layer                   | Technology                                                                  |
| ----------------------- | --------------------------------------------------------------------------- |
| Frontend                | React + Vite + TypeScript, Tailwind CSS                                     |
| Backend                 | Python + FastAPI                                                            |
| Validation              | Pydantic                                                                    |
| ORM                     | SQLAlchemy (Alembic migrations from Phase 1)                                |
| Database                | PostgreSQL                                                                  |
| Delayed actions         | Database records + manual PTP processor; Redis/RQ reserved but not wired    |
| ML                      | scikit-learn `HistGradientBoostingClassifier` (offline experiments only)    |
| LLM                     | Optional Anthropic Messages API; templated/regex simulation by default      |
| Payments                | Signed webhooks; simulated or explicitly enabled Razorpay Test Mode links   |

Deliberately **not** using: microservices, Kafka, Kubernetes, LangGraph,
RAG, or a vector DB. A modular monolith is far more likely to actually ship
in 14 days, and none of those tools solve a problem this project has.

## Running it locally

### 1. Start Postgres + Redis

```bash
docker compose up -d
```

### 2. Backend

```bash
cd backend
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Windows PowerShell uses `Copy-Item .env.example .env` and
`.\.venv\Scripts\Activate.ps1` instead of `cp` and `source`.

Check it: `curl http://localhost:8000/health`

**Exercise the webhook pipeline** without live Razorpay credentials — set
`RAZORPAY_WEBHOOK_SECRET` in `.env` to any string, then from another
terminal:

```bash
export RAZORPAY_WEBHOOK_SECRET=<same value as in .env>
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_1 --amount 4999.50
python scripts/send_test_webhook.py payment.captured --payment-id pay_demo_1 --amount 4999.50
curl http://localhost:8000/cases   # case should show state RECOVERED
```

Run tests: `python -m pytest` (uses a process-unique temporary SQLite DB, blocks
external sockets, and does not use credentials from `.env`).

**See the guardrail + policy engine reason about 100 cases at once:**

```bash
python scripts/run_synthetic_batch.py --count 100
```

Prints the failure-category mix, the action distribution the baseline
policy chose, and confirms every case left the detection/diagnosis states — this is
the Phase 3 exit criteria.

**Inspect why a specific case got the action it did:**

```bash
curl http://localhost:8000/cases/<case-id>
```

Returns the decision (chosen action, alternatives considered, which
guardrails fired), the scheduled action, and the full audit trail.

**See the Payment Link loop — a Razorpay Test Mode (or simulated) link gets
created automatically, then paid:**

```bash
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_2 --amount 1800 --error-reason card_expired
LINK_ID=$(curl -s http://localhost:8000/cases | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['razorpay_payment_link_id'])")
python scripts/send_test_webhook.py payment_link.paid --payment-link-id "$LINK_ID" --amount 1800
curl http://localhost:8000/cases   # case should show state RECOVERED
```

The link is a clearly labeled local simulation by default (`GET /cases/<id>`
shows `"simulated": true`). To call Razorpay, provide Test Mode credentials
and explicitly set `RAZORPAY_API_ENABLED=true`. Live-mode keys are rejected.
Creating a link does not deliver it to a customer.

**Train the recovery-probability model and compare it against the
baseline policy (Phase 5):**

```bash
python -m app.ml.train                        # trains + saves app/ml/artifacts/model.joblib
python scripts/evaluate_policies.py --count 1000
```

The first command prints holdout ROC-AUC/log-loss/accuracy. The second
runs 1,000 matched synthetic scenarios through both the baseline and ML
policies and prints a side-by-side comparison (revenue recovered, contact
interventions, escalations, notional action cost, incremental ₹) — labeled as a synthetic benchmark, not a
production claim. The model is gitignored and reproducible; re-run
`train.py` any time (same seed → same model).

**See the full Phase 6 Promise-to-Pay loop** — fail a payment in a
category where the baseline reaches for `CONTACT_CUSTOMER` (auth issues
work well), then reply as the customer:

```bash
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_3 --amount 8000 --error-reason otp_incorrect
CASE_ID=$(curl -s http://localhost:8000/cases | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")

curl -s http://localhost:8000/cases/$CASE_ID | python3 -m json.tool   # see the drafted outbound message

curl -X POST http://localhost:8000/cases/$CASE_ID/customer-reply \
  -H "Content-Type: application/json" -d '{"body": "I'\''ll pay 8000 Friday"}'
# -> case_state: AWAITING_OUTCOME, with a PromiseToPay + a scheduled FOLLOW_UP_PTP

curl -X POST http://localhost:8000/cases/$CASE_ID/customer-reply \
  -H "Content-Type: application/json" -d '{"body": "actually this is wrong, I already paid this"}'
# -> case_state: DISPUTED — recovery stops immediately, even overriding a pending promise
```

Message drafting and reply extraction are simulated by default. Drafting is
templated and extraction uses a documented regex/keyword heuristic (both are
clearly marked `"simulated": true` / `channel: "simulated"`). To call
Anthropic, set `LLM_PROVIDER=anthropic`, provide `LLM_API_KEY`, and explicitly
set `LLM_API_ENABLED=true`. Drafted messages are stored, not delivered.

Promises whose due date has passed and were never fulfilled aren't
checked automatically yet (no live scheduler) — run
`python scripts/process_followups.py` to process them manually.

**Run a measurement experiment (Phase 7) from the command line:**

```bash
curl -X POST http://localhost:8000/experiments -H "Content-Type: application/json" -d '{"count": 500, "seed": 11}'
```

Or open the dashboard (below) and click "Run experiment" — same thing,
with a UI. Either way you get back ₹ at risk/recovered per policy,
recovery rate, contact interventions, escalations, notional action cost,
an incremental-₹ headline, and a
`run_id` you can fetch again (`GET /experiments/{run_id}`) or export as a
case-level CSV (`GET /experiments/{run_id}/export.csv`).

### 3. Frontend

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

Open `http://localhost:5173` — you should see the dashboard shell report
"backend healthy" and an empty cases table.

On Windows PowerShell, create the frontend environment file with
`Copy-Item .env.example .env`.

## Objective and evaluation limits

The adaptive scorer currently maximizes
`P(recovery | context, action) * amount - notional_action_cost`. Its INR 5/15/50
cost proxies are small relative to many case amounts, so this is primarily a
recovered-value objective, not a calibrated multi-objective definition of
"least customer friction." The experiment's contact count means selected
contact-type actions, including Payment Link creation; it is not a delivery
count. The experiment reports those selections and
escalations beside revenue, plus realized notional net value; it does not hide
a revenue gain that requires more outreach. Selecting a real friction tradeoff
requires merchant/customer research and explicit policy choices, not tuning
synthetic weights until a chart looks favorable.

Training labels and benchmark outcomes come from the same hand-authored
synthetic simulator. These runs validate code and assumptions, not production
lift or model generalization to real Razorpay traffic.

## Scheduling reality

`WAIT` and `WAIT_FOR_NATIVE_RETRY` create `SCHEDULED` database rows and put the
case in `WAITING`; no process wakes or re-evaluates them. `FOLLOW_UP_PTP` is
processed only when `python scripts/process_followups.py` is run manually or by
an external cron. Redis/RQ are reserved dependencies and available in the local
Compose file, but no current application module imports them.

## 14-day phase plan

| Phase                                     | Days   | What ships                                                                                                   | Why this order                                                                   |
| ----------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| 0. Scope lock + skeleton                  | 1      | Repo, README, architecture, backend, frontend, schema,`.env`                                               | Architecture before AI prevents a rebuild later                                  |
| 1. Razorpay event backbone                | 2–3   | Test-mode integration, webhook endpoint, signature validation, idempotent event persistence                  | Every later feature depends on trustworthy payment state                         |
| 2. Failure intelligence + state machine   | 4–5   | Failure taxonomy, canonical case states, deterministic diagnosis, late-capture handling                      | No point building ML before input/state semantics are correct                    |
| 3. Guardrails + baseline recovery engine  | 6–7   | Allowed-action set, contact limits, cooldowns, stopping rules, baseline policy, scheduled-action records     | Worker-based scheduling remains unimplemented                                    |
| 4. Real recovery action                   | 8      | Razorpay Payment Links integration, action executor, outcome webhook handling                                | Guardrails must exist before the system can do something real                    |
| 5. Adaptive policy / ML                   | 9–10  | Synthetic history generator, recovery-probability model, expected-value scorer, baseline-vs-model comparison | Data/state/action pipeline already works, so ML plugs into a functioning product |
| 6. Promise-to-Pay + LLM                   | 11–12 | Customer conversation simulator, structured intent extraction, PTP parser, dispute detection                 | LLM comes late — it's a tool, not the architecture                              |
| 7. Measurement + explainability dashboard | 13     | Batch runner, ₹ at risk / recovered, incremental lift, decision explanations, audit timeline                | This is how a reviewer sees the system actually created value                    |
| 8. Reliability + submission               | 14     | Failure injections, tests, architecture diagram, deployment, 5-minute demo video                             | A complete system beats a 90%-done fancy one                                     |

Full detail, including the failure taxonomy, action space, and the
baseline-vs-RecoveryOS evaluation design, is in
[`ARCHITECTURE.md`](./ARCHITECTURE.md).

## Repo layout

```
recoveryos/
├── README.md
├── ARCHITECTURE.md
├── docker-compose.yml          # local Postgres + Redis
├── backend/
│   ├── alembic/                 # schema migrations (owns the DB schema from Phase 1 on)
│   │   └── versions/
│   ├── app/
│   │   ├── main.py             # FastAPI app + router registration
│   │   ├── models.py           # SQLAlchemy schema (9 tables)
│   │   ├── core/
│   │   │   ├── config.py       # env-driven settings
│   │   │   ├── database.py     # engine/session
│   │   │   └── security.py     # webhook signature verification
│   │   ├── services/
│   │   │   ├── orchestrator.py       # event/reply → RevenueCase state transitions
│   │   │   ├── failure_diagnosis.py  # deterministic failure taxonomy
│   │   │   ├── policy_engine.py      # guardrails + baseline recovery policy
│   │   │   ├── razorpay_client.py    # Payment Links API (live or simulated)
│   │   │   ├── action_executor.py    # creates links or stores drafted messages
│   │   │   ├── ml_policy.py          # expected-value policy (offline eval only, not live)
│   │   │   ├── llm_client.py         # message drafting + PTP extraction (live or simulated)
│   │   │   ├── ptp_extractor.py      # validates an LLM's promise extraction before it's recorded
│   │   │   ├── ptp_followup.py       # processes due/broken promises
│   │   │   └── experiment_runner.py  # persisted baseline-vs-adaptive experiment runs
│   │   ├── ml/
│   │   │   ├── ground_truth.py       # synthetic P(recovery) simulator + assumptions
│   │   │   ├── synthetic_history.py  # training data generator
│   │   │   ├── costs.py              # per-action notional cost table
│   │   │   ├── train.py              # trains + saves the recovery-probability model
│   │   │   ├── scorer.py             # model → expected-value action ranking
│   │   │   └── artifacts/            # trained model.joblib (gitignored, reproducible)
│   │   └── routers/
│   │       ├── health.py
│   │       ├── cases.py              # GET /cases, GET /cases/{id}, POST /cases/{id}/customer-reply
│   │       ├── webhooks.py           # POST /webhooks/razorpay
│   │       └── experiments.py        # POST/GET /experiments, GET .../export.csv
│   ├── scripts/
│   │   ├── send_test_webhook.py      # signs + sends a local test webhook
│   │   ├── run_synthetic_batch.py    # Phase 3 exit criteria: 100 synthetic cases
│   │   ├── evaluate_policies.py      # Phase 5 exit criteria: baseline vs ML comparison
│   │   └── process_followups.py      # Phase 6: manually process due PTP follow-ups
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── App.tsx             # dashboard shell
│   │   ├── ExperimentPanel.tsx # Phase 7: one-click experiment + CSV download
│   │   ├── api.ts              # typed API client
│   │   └── main.tsx
│   └── .env.example
└── data/                       # synthetic dataset generator lands here (Phase 5)
```
