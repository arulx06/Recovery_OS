# RecoveryOS — Adaptive Revenue Recovery Controller

Built for the **Razorpay AI Buildathon**, Track 03 (AI Revenue Recovery).

> When revenue fails, RecoveryOS decides whether to wait, retry, contact the
> customer, send a payment link, collect a promise-to-pay, escalate, or
> stop — and measures which decisions actually recover the most money with
> the least customer friction.

This is intentionally **not** `failure → LLM → WhatsApp message → payment link`. Razorpay already ships subscription retries and recovery agents that
do variations of that. RecoveryOS answers a narrower, harder question:
*given that revenue is at risk, what is the single best next intervention —
including doing nothing?* See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for
the full pipeline, state machine, and a table of what Razorpay already
provides versus what this project adds on top.

## Status: Phase 7 — Measurement + explainability dashboard (Day 13)

Phases 0–6 (skeleton, webhook backbone, failure taxonomy, guardrails +
baseline policy, real Payment Link execution, adaptive ML policy,
Promise-to-Pay + LLM) are done — see ARCHITECTURE.md for what shipped
there.

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
- [X] `GET /cases/{id}` (from Phase 3/6) already serves the
  decision-explanation and audit-timeline half of "explainability" —
  Phase 7 didn't need to rebuild that, only add the aggregate
  measurement view on top
- [X] 12 new tests (reproducibility with a fixed seed, summary shape,
  CSV row counts, 400/404 edge cases) — 218 backend tests total, all
  passing

**Exit criteria for Phase 7:** one click runs a 500+ case experiment with
metrics reproducible from a fixed seed, plus a downloadable audit. Verified
live: the same seed against the same trained model produces bit-identical
results on repeat runs (confirmed by running `POST /experiments` twice in
a row with `seed: 11` and diffing the response). One caveat worth stating
plainly: reproducibility is scoped to *a fixed trained model* —
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
| Queue / delayed actions | Redis + RQ (from Phase 3)                                                   |
| ML                      | scikit-learn / XGBoost (from Phase 5)                                       |
| LLM                     | Provider-agnostic tool-calling model, structured output only (from Phase 6) |
| Payments                | Razorpay Test Mode REST APIs, Webhooks, Payment Links                       |

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
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

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

Run tests: `pytest` (uses an isolated sqlite DB, no Postgres needed).

**See the guardrail + policy engine reason about 100 cases at once:**

```bash
python scripts/run_synthetic_batch.py --count 100
```

Prints the failure-category mix, the action distribution the baseline
policy chose, and confirms every case reached a resolved state — this is
the Phase 3 exit criteria.

**Inspect why a specific case got the action it did:**

```bash
curl http://localhost:8000/cases/<case-id>
```

Returns the decision (chosen action, alternatives considered, which
guardrails fired), the scheduled action, and the full audit trail.

**See the full Phase 4 loop — a real (or simulated) Payment Link gets
created automatically, then paid:**

```bash
python scripts/send_test_webhook.py payment.failed --payment-id pay_demo_2 --amount 1800 --error-reason card_expired
LINK_ID=$(curl -s http://localhost:8000/cases | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['razorpay_payment_link_id'])")
python scripts/send_test_webhook.py payment_link.paid --payment-link-id "$LINK_ID" --amount 1800
curl http://localhost:8000/cases   # case should show state RECOVERED
```

Without `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` set, the link is a clearly
labeled local simulation (`GET /cases/<id>` shows `"simulated": true` on
the action's result) — set real test-mode credentials in `.env` to switch
to live Razorpay Payment Links with no code change.

**Train the recovery-probability model and compare it against the
baseline policy (Phase 5):**

```bash
python -m app.ml.train                        # trains + saves app/ml/artifacts/model.joblib
python scripts/evaluate_policies.py --count 1000
```

The first command prints holdout ROC-AUC/log-loss/accuracy. The second
runs 1,000 matched synthetic scenarios through both the baseline and ML
policies and prints a side-by-side comparison (revenue recovered, contacts,
escalations, incremental ₹) — labeled as a synthetic benchmark, not a
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

Without `LLM_API_KEY` set, message drafting is templated and reply
extraction is a documented regex/keyword heuristic (both clearly marked
`"simulated": true` / `channel: "simulated"`) — set a real Anthropic API
key in `.env` to switch to live calls with no code change.

Promises whose due date has passed and were never fulfilled aren't
checked automatically yet (no live scheduler) — run
`python scripts/process_followups.py` to process them manually.

**Run a measurement experiment (Phase 7) from the command line:**

```bash
curl -X POST http://localhost:8000/experiments -H "Content-Type: application/json" -d '{"count": 500, "seed": 11}'
```

Or open the dashboard (below) and click "Run experiment" — same thing,
with a UI. Either way you get back ₹ at risk/recovered per policy,
recovery rate, contacts, escalations, an incremental-₹ headline, and a
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

## 14-day phase plan

| Phase                                     | Days   | What ships                                                                                                   | Why this order                                                                   |
| ----------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| 0. Scope lock + skeleton                  | 1      | Repo, README, architecture, backend, frontend, schema,`.env`                                               | Architecture before AI prevents a rebuild later                                  |
| 1. Razorpay event backbone                | 2–3   | Test-mode integration, webhook endpoint, signature validation, idempotent event persistence                  | Every later feature depends on trustworthy payment state                         |
| 2. Failure intelligence + state machine   | 4–5   | Failure taxonomy, canonical case states, deterministic diagnosis, late-capture handling                      | No point building ML before input/state semantics are correct                    |
| 3. Guardrails + baseline recovery engine  | 6–7   | Allowed-action set, contact limits, cooldowns, stopping rules, simple baseline policy, scheduler             | Need a safe, working product before layering AI on top                           |
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
│   │   │   ├── action_executor.py    # turns a decision into a real side effect
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
