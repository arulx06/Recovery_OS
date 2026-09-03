# RecoveryOS — Architectural Decision Records

> Lightweight, append-only ADRs. Only decisions supported by the current codebase are recorded here — no invented history. Copy the template for new decisions (see `docs/DEVELOPMENT.md` Definition of Done). Status values: `Accepted` / `Deprecated` / `Superseded`.

---

## ADR-01 — Modular monolith over microservices

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-21 (Day 1 skeleton — see `backend/app/main.py` single FastAPI app) |
| **Context** | Buildathon track requires shipping in 14 days with credible revenue-recovery measurement, not an infrastructure showcase. Split services (payment ingestion, policy, ML, LLM) have no independent deploy/scale owner. |
| **Decision** | Ship a modular monolith: one FastAPI app (`app/main.py:12`), one Postgres, one React build. Modules separated by import boundaries (`services/orchestrator`, `services/policy_engine`, `services/ml_policy`, `services/llm_client`) rather than processes or RPC. |
| **Why** | Faster local bring-up (`docker compose up -d` + `uvicorn`), single migration history, easier atomic transactions (webhook → diagnosis → decision → action in one DB commit), no inter-service auth/secrets to rotate. |
| **Consequences** | Positive: phase order composable (each phase depends on previous phase being trustworthy); simple CI. Negative: no independent deploy of ML scorer; `WAIT` wake-up must be added carefully to avoid synchronous hot-path contention. |
| **Reference** | `README.md: Tech stack "Deliberately not using: microservices, Kafka, Kubernetes"`; `ARCHITECTURE.md: Future components` |

---

## ADR-02 — PostgreSQL as authoritative state; Redis only as transport

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-21 (schema lock `app/models.py:1`, `docker-compose.yml:7` Postgres required) |
| **Context** | Revenue decisions must be durable, explainable, and auditable (`AuditEvent`, `Decision`, `Action` append-only). A Redis-as-state design would lose state on eviction/restart and force dual-write complexity. |
| **Decision** | PostgreSQL owns `RevenueCase`, `PaymentEvent`, `Decision`, `Action`, `PromiseToPay`, `AuditEvent`, and `ExperimentCase`. Redis/RQ carries only `action_id`; workers re-read schedule, status, attempt budget, linkage, and case state from PostgreSQL before acting. |
| **Why** | Auditability and crash safety outrank queue throughput at this scale; `payment_events.razorpay_event_id` unique already handles concurrency; transactional webhook→diagnosis→decision in one commit is natural in Postgres. |
| **Consequences** | Positive: Redis loss is repaired by DB reconciliation and tests can disable the transport. Negative: operators must run both a scheduled worker and reconciliation; queue metadata is observability rather than a second source of truth. |
| **Reference** | `app/models.py:9`, `app/core/database.py`, `docs/INTEGRATIONS.md: Redis / RQ`, `docs/CURRENT_STATE.md: Runtime reality` |

---

## ADR-03 — Deterministic guardrails bound every decision, including ML-chosen ones

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-23 (guardrails landed `policy_engine.py:68` `GuardrailConfig`; ML policy added `ml_policy.py:71` reuse) |
| **Context** | A learned policy could propose arbitrary contact or links; contacting a customer for a `₹100,000` failed payment ten times has legal/goodwill cost that outweighs a model score. |
| **Decision** | Every decision — baseline or adaptive — passes through `policy_engine.check_action_allowed` (amount cap `max_automated_amount=25000`, contact limits `max_contacts_per_case=3`, rolling window `max_contacts_per_7_days=2`, cooldown `12h`, stopping rule `max_total_attempts=5`). `ml_policy.decide_ml` reuses `record_decision` + `ACTION_TO_STATE`, never bypassing guardrails. |
| **Why** | Guardrails are the single safety rail that makes "offline experiments are safe" true. The scorer never picks an action; it only ranks the guardrail-allowed set — see `app/ml/scorer.py:99` `expected_value = P̂·amount − cost`. |
| **Consequences** | Positive: adaptive policy is intrinsically safe; synthetic contact inflation (474 vs 253 in 1000-scenario run) is directly observable as `contacts_made` because guardrails emit `alternatives: {action: {allowed, reason}}`. Negative: some ML-preferred actions are currently muted by `max_automated_amount` → conservative `ESCALATE` on large invoices — desired but worth documenting. |
| **Reference** | `app/services/policy_engine.py:107`, `app/services/ml_policy.py:35` |

---

## ADR-04 — LLM never controls money-moving decisions

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-21 (pitch line "Money-moving decisions are made by deterministic code" baked into `ARCHITECTURE.md:60`) |
| **Context** | LLMs are good at language and bad at being consistently correct about money; an LLM that hallucinates "pay now" into a policy decision would be a loss of auditability and a liability. |
| **Decision** | The LLM (`app/services/llm_client.py`) is restricted to: (a) drafting one short, compliant outbound message given `{amount, failure_category, action_type}`, and (b) classifying one inbound message into `{promise_to_pay, dispute, unclear} + {amount, date, confidence}`. Both are wrapped by non-LLM checks: drafting failures fall back to templated text + flag `simulated=true`; extraction is validated by `ptp_extractor.validate_promise` before any `PromiseToPay` is recorded. Disputes go through the same `_dispute_case` path as Razorpay's `payment.dispute.created`. |
| **Why** | Keeps action authority in `orchestrator.py`/`policy_engine.py` and preserves the append-only `AuditEvent` trail; the interaction that judges can audit is "LLM guessed → deterministic rule checked → deterministic state change", not "LLM decided". |
| **Consequences** | Positive: `action_executor.perform` can be exercised hermetically with `LLM_API_ENABLED=false`. Negative: outbound copy quality is templated-by-default; improving tone requires explicit enablement of the Anthropic path, not a new model position. |
| **Reference** | `app/services/llm_client.py:12`, `app/services/ptp_extractor.py:38`, `ARCHITECTURE.md: LLM boundary` |

---

## ADR-05 — Explicit external API enablement; simulation is the default

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-22 (`razorpay_client.py:65` gate landed with Payment Links; `llm_client.py:109` parallel gate for Anthropic) |
| **Context** | Non-empty credentials in `.env` are easy to leave on a developer machine; turning them into implicit live calls during `pytest` would silently hit external providers and leak secrets into tests. |
| **Decision** | No external traffic occurs unless the **explicit** flag `*_API_ENABLED=true` is set — regardless of whether `*_KEY_ID`/`*_KEY` is non-empty. Simulation (`plink_sim_*` with `simulated=true` / `_TEMPLATES` or regex extraction) is the default. Live gates further enforce `RAZORPAY_KEY_ID.startswith("rzp_test_")` and `LLM_PROVIDER==anthropic`. |
| **Why** | Tests can ship fake but non-empty keys (`conftest.py:13` `rzp_test_fake_hermetic`) without fear; CI never needs a secret; the `payment_link.paid` recovery path is exercised via the same handler for simulated and genuine links. |
| **Consequences** | Positive: `tests/test_razorpay_client.py:101` "Non-blank credentials do not enable network implicitly" is an enforced invariant. Negative: developers must remember to set the flag **and** restart `uvicorn` — two conditions rather than one to get live Test Mode. |
| **Reference** | `app/services/razorpay_client.py:48`, `app/services/llm_client.py:56`, `backend/.env.example:8`, `docs/INTEGRATIONS.md` |

---

## ADR-06 — Razorpay Test Mode only during development

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-22 (`razorpay_client.py:72` `rzp_test_*` gate) |
| **Context** | Buildathon evaluation is against Razorpay Test Mode API; production live-money is out of scope but the codebase must prevent misleading "it works" claims or accidental live calls. |
| **Decision** | `create_payment_link` rejects any non-`rzp_test_*` key at call time with `RazorpayAPIError("RecoveryOS only permits Razorpay Test Mode API keys")`, even when `RAZORPAY_API_ENABLED=true`. Docs state `TEST MODE ≠ production` as a top-level disclaimer (`README.md: Two disclaimers`, `docs/INTEGRATIONS.md: TEST MODE ≠ production`). |
| **Why** | Verification path needed to demonstrate the real Razorpay REST call (`POST /v1/payment_links` → genuine `plink_*`) without moving real money; production readiness is a separate future phase with explicit environment/secret management. |
| **Consequences** | Positive: the manually observed `plink_*` / `rzp.io` path cited in this pass is explicitly Test Mode and labelled. Negative: a production cut-over would touch `razorpay_client.py`, `config.py`, and `INTEGRATIONS.md` at minimum — not a one-line flip. |
| **Reference** | `app/services/razorpay_client.py:48`, `docs/CURRENT_STATE.md: Capability matrix footnote` |

---

## ADR-07 — Synthetic evaluation must be labeled everywhere it appears

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-23 (`app/ml/ground_truth.py:1` "This is NOT a claim about real recovery rates") |
| **Context** | Until live outcome logs exist, training and evaluation share the same `ground_truth` simulator — the model's only claim is "I reconstruct the simulator's hand-set probabilities better than the fixed policy does for a synthetic population." Presenting that as production lift would be dishonest. |
| **Decision** | Every synthetic surface is labeled: file docstrings ("SYNTHETIC SIMULATION BENCHMARK", `ground_truth.py:16`, `evaluate_policies.py:12`, `experiment_runner.py:4`), HTTP/JSON contracts (`ExperimentPanel.tsx:75` banner, `README` disclaimer), CLI echo (`evaluate_policies.py:148` "SYNTHETIC SIMULATION BENCHMARK — not production Razorpay lift"), and `docs/ML_AND_EVALUATION.md` circularity section. The 1000-scenario adaptive-vs-baseline comparison is always reported alongside **both** `revenue recovered` and `contacts made` — the revenue↑+contacts↑ tradeoff is first-class, not a footnote. |
| **Why** | Keeps reviewers and future phases honest about `BASE_PROBABILITY` sensitivity and `ACTION_COST` non-calibration; prevents synthetic-weight tuning to look favorable. |
| **Consequences** | Positive: the `Incremental gross recovered (synthetic)` headline in the dashboard/API is always accompanied by the per-arm contact and cost rows, plus a downloadable audit CSV. Negative: the synthetic numbers cannot be used to promise lift on real traffic. |
| **Reference** | `app/ml/ground_truth.py:33` hand-set table, `docs/ML_AND_EVALUATION.md: Shared simulator for train and evaluate` |

---

## ADR-08 — Tests must be hermetic and network-denying

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-22 (`tests/conftest.py:56` socket guard landed) |
| **Context** | The project has real external providers (Razorpay, Anthropic). Allowing tests to reach out would make CI flaky, leak credentials, and hide the fact that most paths have a `simulated` branch suitable for hermetic coverage. |
| **Decision** | Tests run against a temp SQLite file (`conftest.py:8`), force `RAZORPAY_API_ENABLED=false` / `LLM_API_ENABLED=false` with fake keys, and monkey-patch `socket` to allow only loopback connections (`conftest.py:56`). `httpx.post` is monkeypatched in `test_razorpay_client.py` / `test_llm_client.py` to assert live-path shape without network. DB isolation is per-function (`tests/conftest.py:46` `delete()` all tables after every test). |
| **Why** | Verification of Test Mode / live LLM is a manual job that coexists with passing CI — `automated tests are expected to remain isolated from external APIs` is an invariant of this codebase. |
| **Consequences** | Positive: the full suite passes on any machine with no `.env` secrets; manual verification in the runbook is the only place live calls are documented. Negative: live shape after a provider API change has to be caught by manual integration checks, not CI. |
| **Reference** | `tests/conftest.py`, `tests/test_razorpay_client.py:53`, `tests/test_llm_client.py:49` |

---

## ADR-09 — Model artifact is gitignored and reproducible from repo

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-23 (`backend/.gitignore:8` `app/ml/artifacts/`) |
| **Context** | The model is a derived artifact (weights + preprocessor) from synthetic history; committing it would pin a specific build without pinning the simulator assumptions that produced it, and would make judging reproducibility harder (`git diff` on a binary). |
| **Decision** | Artifact lives at `backend/app/ml/artifacts/model.joblib` (written by `python -m app.ml.train`), gitignored, reproduced deterministically via `train.py` with `--n` + `--seed`. CI trains from scratch each run (`ci.yml: Train recovery-probability model`). `scorer._load` is protected by a `stat(mtime,size)` cache; tests use an isolated `tmp_path_factory` artifact. |
| **Why** | Keeps the repository source-first; the trained artifact is small (KBs) but intentionally rebuildable. |
| **Consequences** | Positive: a fresh clone must `python -m app.ml.train` before any `/experiments` or `evaluate_policies.py` call — missing model is a `503 ModelNotTrainedError` with a clear message. Negative: the CI step adds seconds to the build. |
| **Reference** | `backend/.gitignore:8`, `app/ml/train.py:37`, `app/ml/scorer.py:38`, `ci.yml` |

---

## ADR-10 — Durable action runtime uses Redis only as transport

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-03 (this documentation pass) |
| **Context** | Delayed waits and PTP deadlines must survive request completion, process restarts, duplicate delivery, and Redis loss without letting stale work reopen recovered/disputed cases. Provider calls must not run inside webhook transactions. |
| **Decision** | `Action` is the durable job record. Requests commit it before publishing. RQ jobs contain only `action_id` and use deterministic IDs per attempt. A worker locks the case and conditionally claims `SCHEDULED -> EXECUTING`, commits before external I/O, and finalizes in a new transaction after rechecking state. Expected failures retry with bounded exponential delay; stale claims use a configurable lease. `scripts/reconcile_actions.py` resets expired claims and republishes every scheduled queueable action. |
| **Why** | A separate job table would duplicate the existing action lifecycle. Redis-as-authority creates a dual-write loss window. Holding a DB transaction across provider I/O increases lock time and still cannot provide provider exactly-once semantics. |
| **Consequences** | Positive: DB-bound replay, duplicate-worker exclusion, sanitized failures, delayed execution, and observable claims. Negative: at-least-once transport still has a narrow provider accepted/request finalization crash window; Payment Links therefore use deterministic action references and need future provider reconciliation for absolute closure. Windows requires `app.worker.WindowsWorker` because stock RQ 1.16.2 assumes `fork`/`SIGALRM`. |
| **Reference** | migration `8d6e24f91a73`, `app/services/temporal_runtime.py`, `app/services/task_queue.py`, `tests/test_temporal_runtime.py` |

---

## ADR-11 — Date-only promises remain valid through the merchant-local day

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-03 |
| **Context** | Customer text usually provides a date, not an instant. Treating `YYYY-MM-DD` as midnight UTC marks an India-local promise broken before that day has meaningfully begun. Selecting the latest pending promise also lets one follow-up break a different promise. |
| **Decision** | Each `FOLLOW_UP_PTP` stores `promise_to_pay_id`. Its `scheduled_for` is the exclusive end of the promised date in `MERCHANT_TIMEZONE`, converted to naive UTC for the existing schema. New promises mark old pending promises `SUPERSEDED` and cancel their follow-ups. Recovery marks pending promises `KEPT`. Historical unlinked actions are repaired only if one pending promise is unambiguous. |
| **Consequences** | Business dates behave consistently across UTC boundaries and follow-ups mutate only their intended promise. Changing merchant timezone after scheduling does not rewrite existing deadlines. |
| **Reference** | `app/core/time.py`, `app/services/orchestrator.py`, `app/services/ptp_followup.py`, `tests/test_temporal_runtime.py` |

---

## ADR-12 — External side effects require provider reconciliation; DB claim alone is not exactly-once

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-03 |
| **Context** | `POST /v1/payment_links` can commit on Razorpay's side while the worker crashes or the response is lost before DB finalization. The previous durable runtime correctly used `SCHEDULED → EXECUTING` atomic claims, deterministic `Action.id`→`reference_id`, and bounded retries, but a `POST`-accepted/response-lost window still allowed a retry to `POST` again and create a second `plink_*`. No `Idempotency-Key` header exists for Payment Links; only `reference_id` uniqueness and `GET /v1/payment_links?reference_id=` filtering are documented. |
| **Decision** | Extend `CREATE_PAYMENT_LINK` with provider-side reconciliation: (1) canonical stable identity `reference_id = Action.id` via `build_provider_reference()`; (2) error classification — `Timeout`/`NetworkError`/`5xx`/`429`/duplicate-`reference_id` → `RazorpayAmbiguousError` (reconcile before retry), other `4xx` → `RazorpayAPIError` (bounded retry); (3) on `RazorpayAmbiguousError` and on stale `EXECUTING` lease expiry, `GET /v1/payment_links?reference_id=Action.id` outside any DB transaction, validate `reference_id`/`amount`/`currency`/`notes`/`status` (reject `expired`/`cancelled`, mismatch → `HUMAN_REVIEW`), and either adopt via canonical `apply_success(_reconciled=true)` → `action_reconciled` or retry only when reliably absent; lookup itself ambiguous → bounded `payment_link_reconciliation_pending` retry; all provider I/O remains outside long DB transactions; results sanitized via `_sanitize_link()`. Add manual `scripts/reconcile_payment_links.py`. |
| **Why** | DB claim prevents concurrent local execution, but only a provider `list?reference_id` can tell whether Razorpay already committed. Duplicate `reference_id` returns `400` — the documented idempotency primitive — so the safe path is to `GET` before any second `POST`. `expired`/`cancelled` and mismatched `amount`/`currency`/`notes` must not be silently adopted. |
| **Consequences** | Positive: at-least-once worker claim + `reference_id` uniqueness + validated `GET` closes the crash-after-accept window without duplicates; stale `EXECUTING` reconciles before lease reset; Redis loss preserves ambiguity (republishes without immediate second `POST`); simulation remains `simulated=true` default; audit trail distinguishes `action_executed` vs `action_reconciled`. Negative: one extra `GET` per ambiguous attempt (bounded by `max_attempts`), one-time `GET` per stale payment-link claim; remaining exactly-once guarantee is bounded by Razorpay's `reference_id` uniqueness, not by DB alone — documented as such. |
| **Reference** | `app/services/razorpay_client.py:140` + `find_payment_link_for_action`, `app/services/temporal_runtime.py:234`, `app/services/action_executor.py:88`, `scripts/reconcile_payment_links.py`, `tests/test_payment_link_reconciliation.py`, `docs/INTEGRATIONS.md: Razorpay` + `docs/SYSTEM_FLOWS.md: 2a–2c` |

---

## Proposed template for future ADRs

```markdown
## ADR-NN — <title>

| Field | Value |
|-------|-------|
| **Status** | Proposed / Accepted / Deprecated / Superseded by ADR-MM |
| **Date** | YYYY-MM-DD |
| **Context** | What problem forced the decision |
| **Decision** | What is being decided, precisely |
| **Why** | Alternatives considered and why they lost |
| **Consequences** | Positive and negative, including migrations/docs/tests required |
| **Reference** | Files, flows, capability-matrix rows affected |
```

New ADRs must be cited from at least one of `ARCHITECTURE.md` or `docs/CURRENT_STATE.md` so the matrix and the reasoning stay coupled.
