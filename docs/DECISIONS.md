# RecoveryOS — Architectural Decision Records

> Lightweight, append-only ADRs. Only decisions supported by the current codebase are recorded here — no invented history. Copy the template for new decisions (see `docs/DEVELOPMENT.md` Definition of Done). Status values: `Accepted` / `Deprecated` / `Superseded`.

---

## ADR-01 — Modular monolith over microservices

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-21 |
| **Context** | Buildathon track requires shipping in 14 days with credible revenue-recovery measurement, not an infrastructure showcase. Split services (payment ingestion, policy, ML, LLM) have no independent deploy/scale owner. |
| **Decision** | Ship a modular monolith: one FastAPI app (`app/main.py:12`), one Postgres, one React build. Modules separated by import boundaries (`services/orchestrator`, `services/policy_engine`, `services/ml_policy`, `services/llm_client`) rather than processes or RPC. |
| **Why** | Faster local bring-up (`docker compose up -d` + `uvicorn`), single migration history, easier atomic transactions (webhook → diagnosis → decision → action in one DB commit), no inter-service auth/secrets to rotate. |
| **Consequences** | Positive: simple CI, atomic transactions, and straightforward local operation. Negative: no independent deployment of the ML scorer; delayed `WAIT` execution requires the worker and scheduler. |
| **Reference** | `backend/app/main.py`, `ARCHITECTURE.md` |

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
| **Consequences** | Positive: adaptive policy remains bounded, and contact frequency is observable through `contacts_made` and guardrail alternatives. Negative: some model-preferred actions are muted by `max_automated_amount`, resulting in conservative escalation for large invoices. |
| **Reference** | `app/services/policy_engine.py:107`, `app/services/ml_policy.py:35` |

---

## ADR-04 — LLM never controls money-moving decisions

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-21 (pitch line "Money-moving decisions are made by deterministic code" baked into `ARCHITECTURE.md:60`) |
| **Context** | LLMs are good at language and bad at being consistently correct about money; an LLM that hallucinates "pay now" into a policy decision would be a loss of auditability and a liability. |
| **Decision** | The LLM (`app/services/llm_client.py`) is restricted to: (a) drafting one short, compliant outbound message given `{amount, failure_category, action_type}`, and (b) classifying one inbound message into `{promise_to_pay, dispute, unclear} + {amount, date, confidence}`. Both are wrapped by non-LLM checks: drafting failures fall back to templated text + flag `simulated=true`; extraction is validated by `ptp_extractor.validate_promise` before any `PromiseToPay` is recorded. Disputes go through the same `_dispute_case` path as Razorpay's `payment.dispute.created`. |
| **Why** | Keeps action authority in `orchestrator.py`/`policy_engine.py` and preserves the append-only `AuditEvent` trail; the auditable interaction is "LLM proposed → deterministic rule checked → deterministic state change", not "LLM decided". |
| **Consequences** | Positive: `action_executor.perform` can be exercised hermetically with `LLM_API_ENABLED=false`. Negative: outbound copy quality is templated by default; provider-backed drafting requires explicit enablement. |
| **Reference** | `app/services/llm_client.py:12`, `app/services/ptp_extractor.py:38`, `ARCHITECTURE.md: LLM boundary` |

---

## ADR-05 — Explicit external API enablement; simulation is the default

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-22 (`razorpay_client.py:65` gate landed with Payment Links; `llm_client.py:109` parallel gate for Anthropic) |
| **Context** | Non-empty credentials in `.env` are easy to leave on a developer machine; turning them into implicit live calls during `pytest` would silently hit external providers and leak secrets into tests. |
| **Decision** | No external traffic occurs unless the **explicit** flag `*_API_ENABLED=true` is set, regardless of whether credentials are present. Simulation is the default. Razorpay additionally requires an `rzp_test_*` key; LLM access requires a supported provider (`anthropic` or `opencode_zen`). |
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
| **Decision** | `create_payment_link` rejects any non-`rzp_test_*` key at call time with `RazorpayAPIError("RecoveryOS only permits Razorpay Test Mode API keys")`, even when `RAZORPAY_API_ENABLED=true`. The README and integration guide clearly separate Test Mode from production operation. |
| **Why** | The Test Mode path verifies the Razorpay REST call (`POST /v1/payment_links` → genuine `plink_*`) without moving real money. Production operation would require separate credentials, environment controls, and secret management. |
| **Consequences** | Positive: any future `plink_*` / `rzp.io` smoke is constrained to Test Mode and labelled; automated coverage remains network-hermetic. Negative: a production cut-over would touch `razorpay_client.py`, `config.py`, and `INTEGRATIONS.md` at minimum — not a one-line flip. |
| **Reference** | `app/services/razorpay_client.py:48`, `docs/CURRENT_STATE.md: Capability matrix footnote` |

---

## ADR-07 — Synthetic evaluation must be labeled everywhere it appears

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-23 (`app/ml/ground_truth.py:1` "This is NOT a claim about real recovery rates") |
| **Context** | Until live outcome logs exist, training and evaluation share the same `ground_truth` simulator — the model's only claim is "I reconstruct the simulator's hand-set probabilities better than the fixed policy does for a synthetic population." Presenting that as production lift would be dishonest. |
| **Decision** | Every synthetic surface is labeled: file docstrings ("SYNTHETIC SIMULATION BENCHMARK", `ground_truth.py:16`, `evaluate_policies.py:12`, `experiment_runner.py:4`), HTTP/JSON contracts (`ExperimentPanel.tsx:75` banner, `README` disclaimer), CLI echo (`evaluate_policies.py:148` "SYNTHETIC SIMULATION BENCHMARK — not production Razorpay lift"), and `docs/ML_AND_EVALUATION.md` circularity section. The 1000-scenario adaptive-vs-baseline comparison is always reported alongside **both** `revenue recovered` and `contacts made` — the revenue↑+contacts↑ tradeoff is first-class, not a footnote. |
| **Why** | Makes `BASE_PROBABILITY` sensitivity and uncalibrated action costs explicit and discourages tuning synthetic weights solely for favorable results. |
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
| **Date** | 2026-08-23 |
| **Context** | The model is a derived artifact (weights + preprocessor) from synthetic history; committing it would pin a specific build without pinning the simulator assumptions that produced it, and binary diffs would make reproducibility harder to review. |
| **Decision** | Artifact lives at `backend/app/ml/artifacts/model.joblib` (written by `python -m app.ml.train`), gitignored, reproduced deterministically via `train.py` with `--n` + `--seed`. CI trains from scratch each run (`ci.yml: Train recovery-probability model`). `scorer._load` is protected by a `stat(mtime,size)` cache; tests use an isolated `tmp_path_factory` artifact. |
| **Why** | Keeps the repository source-first; the trained artifact is small (KBs) but intentionally rebuildable. |
| **Consequences** | Positive: a fresh clone must `python -m app.ml.train` before any `/experiments` or `evaluate_policies.py` call — missing model is a `503 ModelNotTrainedError` with a clear message. Negative: the CI step adds seconds to the build. |
| **Reference** | `.gitignore`, `app/ml/train.py`, `app/ml/scorer.py`, `.github/workflows/ci.yml` |

---

## ADR-10 — Durable action runtime uses Redis only as transport

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-03 |
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

## ADR-13 — ML ranks safe actions; deterministic guardrails remain authoritative

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-04 |
| **Context** | A friction-unaware adaptive policy can increase customer contact for a comparatively small synthetic recovery gain. Letting ML bypass contact limits, cooldowns, or amount caps would trade customer goodwill for model-favored outreach. |
| **Decision** | Adaptive scores only the guardrail-allowed set (`policy_engine.check_action_allowed`) plus semantic filter (`WAIT_FOR_NATIVE_RETRY` only if `subscription_linked`). `scorer.rank_actions_with_friction` is batched and `policy_dispatcher` is the single dispatch point; `RECOVERY_POLICY=baseline` (default) never loads the model. |
| **Why** | Guardrails are merchant-safety, friction is preference ranking — conflating them would hide blocks as huge penalties. Keeping them separate makes `HUMAN_REVIEW` auditable and prevents ML from spending past limits. |
| **Consequences** | Positive: adaptive cannot bypass hard limits; shadow and adaptive share identical guardrail semantics; fallback remains deterministic. Negative: some model-preferred actions are muted by guardrails (e.g., large amount → `ESCALATE`). |
| **Reference** | `app/services/ml_policy.py`, `app/services/policy_dispatcher.py`, `app/ml/friction.py`, `tests/test_adaptive_policy.py` |

---

## ADR-14 — Baseline is the default and fallback; shadow is side-effect-free

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-04 |
| **Context** | New adaptive must not strand cases on model failure, nor require a second Decision/Action that confuses case state. Teams need a safe rollout path that compares without double-sending links. |
| **Decision** | `RECOVERY_POLICY=baseline` is default and the fallback for `adaptive` on any `ModelNotTrainedError`/`manifest`/`smoke`/`NaN` — emits `adaptive_fallback` audit and delegates to `policy_engine.decide` in the same transaction (never stranded `DIAGNOSED`). `shadow` executes baseline's Decision/Action and only emits `shadow_adaptive_recommendation` (or `shadow_adaptive_error`) audit with `{baseline_chosen, adaptive_suggested, utility, fingerprint, candidates}` — no second Action, queue job, Payment Link, or PTP. |
| **Why** | One dispatcher centralizes mode checks; orchestrator and `temporal_runtime._complete_wait` both call `policy_dispatcher.decide_for_case` so re-diagnosis and WAIT-wake share the flag. Startup-loaded `RECOVERY_POLICY` requires restart to change — no hot-reload races. |
| **Consequences** | Positive: `adaptive` degraded is not `degraded` for baseline health; shadow proves no side effects (`tests/test_adaptive_policy.py`); `RECOVERED`/`STOPPED`/`DISPUTED` remain automation-terminal. Negative: shadow recommendations are audit-only until a dedicated timeline UI exists. |
| **Reference** | `app/services/policy_dispatcher.py`, `app/services/orchestrator.py:191`, `app/services/temporal_runtime.py:160`, `app/models.py:Decision` provenance columns |

---

## ADR-15 — Customer friction is an explicit policy objective, not a hidden cost

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-04 |
| **Context** | `ACTION_COST = {WAIT 0, CREATE 5, CONTACT 15, PTP 15, ESCALATE 50}` is too small vs `amount` to restrain revenue-first adaptive; tuning it until a benchmark looks good would hide the tradeoff. |
| **Decision** | Introduce `friction_score` (`BASE_FRICTION: WAIT 0, WAIT_NATIVE 2, CREATE 25, CONTACT 40, PTP 60, ESCALATE 80` plus `+12 per prior contact` for contact types) and `utility = p*amount - cost - weight*friction` with explicit `PROFILE_WEIGHTS = {revenue_first:4, balanced:18 (default), low_friction:45}` INR per point. Dashboards show `recovered`, `cost`, and `friction` separately. |
| **Why** | Makes intervention frequency a first-class, testable, documentable objective distinct from hard guardrails (`max_contacts`). Weight is a policy preference, not a measured monetary cost — honest about `synthetic_data_notice`. |
| **Consequences** | Positive: profiles make recovery-versus-contact tradeoffs explicit, and `recovered_per_contact` is reported. Negative: no weight is empirically optimal; merchant research and real outcomes must inform any production profile. |
| **Reference** | `app/ml/friction.py`, `app/ml/scorer.py:rank_actions_with_friction`, `app/services/ml_policy.py`, `docs/ML_AND_EVALUATION.md: Friction is a first-class objective` |

---

## ADR-16 — Synthetic policy results are not production lift; training and evaluation remain synthetic but honest

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-04 |
| **Context** | `ground_truth` is the only labeled data; training on it and evaluating on the same assumptions is circular if presented as lift. |
| **Decision** | Keep synthetic `ground_truth`/`synthetic_history` but document circularity, generate Brier score and other holdout metrics per artifact, keep one canonical `features.py` pipeline (`FEATURE_SCHEMA_VERSION=v1`) validated at train and serve, require `manifest.json` with fingerprint and synthetic-data notice, and use common-random matched scenarios. Real validation remains a documented roadmap. |
| **Why** | Reproducible methodology is more useful than a favorable but stale benchmark. Friction-aware evaluation reports contacts, contact rate, friction, and recovered-per-contact alongside recovered amount. |
| **Consequences** | Positive: seeded training prints stable evaluation metrics and a SHA-256 fingerprint for the generated artifact; `GET /health` shows `model_available` without requiring Razorpay; `train_serve_parity` guards drift. The byte fingerprint is build-specific, not a release constant. Negative: synthetic robustness ≠ production validation — still needs logged `Decision` provenance + outcome timestamps for future real learning. |
| **Reference** | `app/ml/train.py`, `app/ml/scorer.py`, `app/ml/features.py`, `app/ml/manifest.py`, `app/services/experiment_runner.py`, `docs/ML_AND_EVALUATION.md` |

---

## ADR-17 — LLM is advisory language intelligence, not recovery controller

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-05 |
| **Context** | LLMs are good at language, bad at consistently correct financial decisions; allowing a model to choose `WAIT` vs `CREATE_PAYMENT_LINK` would be unauditable and a liability. |
| **Decision** | LLM (`app/services/llm_client.py`) is optional downstream support only: (a) draft one `DRAFT` message with `[[PAYMENT_LINK]]` placeholder after controller has chosen `CONTACT_CUSTOMER`/`COLLECT_PROMISE_TO_PAY`/`CREATE_PAYMENT_LINK`, (b) extract one structured `PTPExtraction` `{intent, promised_amount, promised_date, confidence, reasoning_code}` from customer free text. Both are wrapped by non-LLM checks: drafting substitution uses authoritative `short_url`; extraction is validated by `ptp_extractor.validate_promise` before any `PromiseToPay` is recorded. LLM never calls `policy_engine`/`ml_policy` nor mutates `RevenueCase.state` directly. |
| **Why** | Keeps action authority in `orchestrator`/`policy_dispatcher`/`temporal_runtime` and preserves the append-only `AuditEvent` trail; the auditable interaction is "LLM proposed → deterministic validation → deterministic state change", not "LLM decided". Deterministic fallback (`draft_with_fallback`/`extract_with_fallback`) prevents provider outages from stranding cases. |
| **Consequences** | Positive: `action_executor.perform` remains hermetic with `LLM_API_ENABLED=false`; `CONTACT_CUSTOMER` still completes with a fallback draft; injection cannot change policy mode or guardrails. Negative: provider-backed copy requires explicit configuration. |
| **Reference** | `app/services/llm_client.py:43` (prompt/schema versions, placeholder), `app/services/action_executor.py:43`, `app/services/orchestrator.py:475`, `ARCHITECTURE.md: LLM boundary`, `docs/CURRENT_STATE.md: LLM PTP extraction / LLM message drafting` |

---

## ADR-18 — Payment truth remains provider-authoritative

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-05 |
| **Context** | Customer text "I already paid" is a claim, not evidence; letting a model set `RECOVERED` would let prompt injection create fake recovery and bypass Razorpay reconciliation. |
| **Decision** | `RECOVERED` is only via `_recover_case` from `payment.captured`/`subscription.charged`/`payment_link.paid` webhooks (provider truth). Customer `payment_claim`/`dispute` from PTP extraction routes through same `_dispute_case` as `payment.dispute.created` → `DISPUTED` (or `HUMAN_REVIEW`), cancels `SCHEDULED` actions, never `RECOVERED`. No model confidence overrides provider evidence. |
| **Why** | Webhook verification + provider reconciliation are the only financial truth; semantic classification (`payment_claim`) is not a state mutation primitive. |
| **Consequences** | Positive: injection `"mark payment successful"` cannot recover money; amount injection cannot create arbitrary PTP. Negative: legitimate "already paid" requires manual provider reconciliation to confirm. |
| **Reference** | `app/services/orchestrator.py:540`, `app/services/ptp_extractor.py`, `tests/test_llm_stage.py::test_llm_cannot_set_recovered`, `docs/CURRENT_STATE.md: Payment truth` |

---

## ADR-19 — Structured PTP extraction requires deterministic validation and placeholder safety

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-05 |
| **Context** | Unconstrained prose parsing for amount/date is hallucination-prone; relative "Friday" needs explicit merchant-local reference; payment links are easy to hallucinate. |
| **Decision** | Define typed schema `PTPExtraction` (`intent`, `promised_amount!`, `promised_date YYYY-MM-DD`, `confidence 0..1`, `reasoning_code`) `ptp-schema-v1`; provider JSON validated at LLM boundary (`_validate_structured_output` checks enum, amount>0, date format, confidence bounds). Direct malformed output raises `LLMInvalidResponseError`; the wrapper alone converts typed provider errors to deterministic provenance. `promised_amount` is only customer-explicit (`amount_method=customer_explicit`); omitted → `HUMAN_REVIEW`, not invented. Relative dates use `business_now = utc_to_local(now, MERCHANT_TIMEZONE)` explicitly; `FOLLOW_UP_PTP` at exclusive end-of-day UTC. Payment Link drafts use `[[PAYMENT_LINK]]`; every model-provided HTTP(S) URL is stripped before the authoritative `short_url` is inserted exactly once. |
| **Why** | Short machine-readable reason codes + prompt versions make behavior testable; deterministic validation blocks vague "sometime next week" → precise date; placeholder prevents `rzp.io` hallucination; merchant-local day keeps business deadline sane. |
| **Consequences** | Positive: `tests/test_llm_stage.py` freezes reference time; ambiguous → `uncertain`/`HUMAN_REVIEW`; `tests/test_ptp_extractor.py` still passes; provenance (`prompt_version`, `amount_method`, `source_message_id`) audit-ready. Negative: date-only promises currently not auto-inferred to full balance (explicit-only) — conservative but honest. |
| **Reference** | `app/services/llm_client.py:47` (placeholder), `app/services/llm_client.py:493` (validation), `app/services/ptp_extractor.py:38`, `app/models.py:PromiseToPay`, `c9d0e1f2a3b4`, `ARCHITECTURE.md: LLM boundary` |

---

## ADR-20 — Customer communication remains draft-only until a delivery provider exists

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-05 |
| **Context** | Adding Twilio, SendGrid, or WhatsApp without a complete transport would imply delivered messages without consent, opt-out handling, and delivery evidence. |
| **Decision** | All outbound `CustomerMessage` are `status=DRAFT` / `MANUAL_ONLY` (`generation_method=deterministic|llm|llm_fallback_template`, `channel=simulated|llm`, `prompt_version=message-v1`). No SMS/email/WhatsApp call. Dashboard shows `DRAFT / NOT SENT`. Delivery transport is `PLANNED` future component. |
| **Why** | Keeps the product state accurate: users see a stored draft and provenance, not a false `DELIVERED` claim; there is no external side effect to mock or test. |
| **Consequences** | Positive: `action_executor.apply_success` idempotent and always `DRAFT`; no delivery retry needed. Negative: operator must manually share `short_url` until transport exists. |
| **Reference** | `app/models.py:CustomerMessage`, `app/services/action_executor.py:107`, `frontend/src/App.tsx:154`, `docs/CURRENT_STATE.md: Customer delivery` |

---

## ADR-21 — Observability is a derived read model, not a second source of truth

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-05 |
| **Context** | Operators need to follow failure → diagnosis → guardrails → candidate comparison → temporal execution → reconciliation → PTP → recovery, but the system already has persisted tables with provenance. Adding a second timeline table or letting the frontend compute financial truth would create drift and fabricated values. |
| **Decision** | Build `app/services/explainability.py` as a deterministic, read-only derivation layer: `failure_explanation` (taxonomy → human meaning), `normalize_decision` (allowed/blocked + P/EV/friction/utility, missing → null/unavailable), `guardrail_visibility`, `friction_breakdown`, `build_timeline` (chronological merge of PaymentEvent+Decision+Action+Message+PTP+AuditEvent, no duplicate table), `provider_truth_summary` (SIMULATED vs TEST MODE). Backend exposes `GET /dashboard/summary` and enriches `GET /cases` / `GET /cases/{id}`; frontend in `frontend/src/components/**` (Overview/Cases/Experiments/System with `?case=<id>` deep-link) only renders what the read-model returns, never invents scores. |
| **Why** | Keeps PostgreSQL authoritative; avoids GraphQL/analytics duplication; timeline built at read time stays consistent with audit trail; historical Decisions without adaptive fields render “Not recorded” instead of fabricated values; synthetic metrics stay labeled `SYNTHETIC SIMULATION`; no LLM-generated controller explanation. Alternative of persisting a duplicate timeline or letting frontend derive `P*amount` would break auditability and risk inventing lift. |
| **Consequences** | Positive: single coherent `GET /cases/{id}` with timeline/decision inspectors/provider truth, server-side filtered case list, and revenue-vs-friction analysis; `scripts/seed_demo.py` is idempotent and source-scoped. Negative: timeline and current friction surfaces are computed per request and may require caching at larger scale. |
| **Reference** | `app/services/explainability.py`, `app/routers/dashboard.py`, `app/routers/cases.py`, `frontend/src/App.tsx` + `frontend/src/components/**`, `scripts/seed_demo.py`, `tests/test_observability.py`, `docs/CURRENT_STATE.md` capability rows, `ARCHITECTURE.md` read-model diagram, `docs/SYSTEM_FLOWS.md` flows 23–29 |

---

## ADR-22 — Release hardening: trust, not features

| Field | Value |
|-------|-------|
| **Status** | Accepted and implemented |
| **Date** | 2026-09-03 |
| **Context** | The demo environment must tolerate missing network access, invalid keys, and unavailable workers without leaking secrets or overstating system state. |
| **Decision** | Add separate liveness and readiness endpoints, optional operator-token protection, startup configuration validation, structured logging and request correlation, bounded experiments, development-only failure injection, queue orphan recovery, secret-safe Docker contexts, and an offline demo path. |
| **Why** | Keeps PostgreSQL authoritative and Razorpay provider-authoritative; Redis/RQ stays at-least-once transport with stale-`EXECUTING` reconciliation already proven. Smallest correct public-demo protection beats building IAM; header-gated injection beats unauthenticated public crash endpoints; one-time migrate beats raced workers. Synthetic evaluation stays labeled `SYNTHETIC SIMULATION`. |
| **Consequences** | Positive: health responses do not leak secrets, live keys are rejected, reconciliation is idempotent, demo reset is scoped, and the frontend handles API outages explicitly. Negative: production IAM and live-money operation remain out of scope. |
| **Reference** | `app/core/config.py:validate_startup_config`, `app/main.py`, `app/core/middleware.py`, `app/core/logging_config.py`, `app/core/demo_auth.py`, `app/routers/health.py`, `app/routers/admin.py`, `app/services/failure_injection.py`, `docker-compose.prod.yml`, `docs/DEMO_SCRIPT.md`, `docs/OFFLINE_DEMO.md`, `docs/PUBLIC_WEBHOOK.md`, `tests/test_release_hardening.py` |

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
