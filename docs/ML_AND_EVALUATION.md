# RecoveryOS — ML and Evaluation

> Scientifically honest accounting of what is learned, what is simulated, and what must still be proven. The adaptive scorer is **offline/synthetic** — it is **not** the live webhook decision policy. See `docs/CURRENT_STATE.md` capability matrix and `docs/SYSTEM_FLOWS.md` flow 8.

---

## Training data origin

- **No real Razorpay outcome data exists yet.** Until RecoveryOS observes production outcomes, there are no `{context, action, outcome}` labels.
- The sole source of training and evaluation labels is the hand-authored simulator `app/ml/ground_truth.py` (see `docs/SYSTEM_FLOWS.md` flow 1).
- When real append-only outcome logs exist, `synthetic_history.generate_history` is the module replaced — `train.py` accepts any `DataFrame` with `FEATURE_COLUMNS`.

---

## Synthetic generator `app/ml/synthetic_history.py`

```python
generate_history(n=3000, seed=42) → DataFrame[case_id, failure_category, action_taken,
                                              amount, days_overdue, previous_contacts,
                                              subscription_linked, hour, day_of_week, recovered]
```

- **Failure category**: weighted draw from `CATEGORY_WEIGHTS` mirroring a realistic failure mix (balance 22, transient 16, auth 14, invalid instrument 12, … — `synthetic_history.py:33`).
- **`action_taken`**: **uniform** across `policy_engine.ALL_ACTIONS` (not just the baseline's top pick for the category). This deliberately exposes the model to recovery outcomes for actions the baseline would never choose, so it can learn "actually, `CONTACT_CUSTOMER` beats `WAIT` for this category" instead of only confirming the baseline's preferences.
- **Amount**: uniform over `[500, 1000, 2500, 5000, 9999, 15000, 25000, 40000, 75000, 150000]` (INR).
- **`days_overdue` / `previous_contacts`**: skewed toward small values (weights in code), `subscription_linked` enriched for `SUBSCRIPTION_PENDING_NATIVE_RETRY`.
- **`recovered`**: `ground_truth.sample_outcome(category, action_taken, amount, days_overdue, previous_contacts, subscription_linked, rng=Random(seed))` — a single Bernoulli draw from `true_probability`.

### Ground-truth assumptions `app/ml/ground_truth.py:33`

| (category, action) pair | Base `P(recovery)` | Intuition |
|-------------------------|--------------------|-----------|
| `TRANSIENT_INFRASTRUCTURE, WAIT` | 0.78 | Transient gateway blips recover well passively |
| `SUBSCRIPTION_PENDING_NATIVE_RETRY, WAIT_FOR_NATIVE_RETRY` | 0.80 | Best to stay silent while Razorpay retries |
| `INSUFFICIENT_BALANCE, COLLECT_PROMISE_TO_PAY` | 0.55 | Low balance often resolves with a scheduled retry |
| `CUSTOMER_AUTHENTICATION, CONTACT_CUSTOMER` | 0.62 | Auth failures need outreach |
| `INVALID_INSTRUMENT, CREATE_PAYMENT_LINK` | 0.65 | New link is the direct fix for expired/invalid instrument |
| `MANDATE_ISSUE, CONTACT_CUSTOMER` | 0.40 | Mandate problems need human-adjacent work |
| `PERMANENT_HARD_FAILURE, ESCALATE` | 0.20 | Best outcome is still low; no cheap action helps |
| `UNKNOWN, ESCALATE` | 0.25 | Conservative default before a human looks |

All other listed pairs have hand-set values; unlisted `(category, action)` fall back to `0.05`, `STOP` is `0.0`. After the base lookup, context modifiers apply deterministically (`ground_truth.py:114`):

- `amount_factor = max(0.7, 1 − amount/200_000·0.3)` — larger invoices modestly harder to recover.
- `overdue_factor = max(0.4, 1 − days_overdue·0.03)` on passive `WAIT` actions — stale cases benefit less from waiting.
- `contact_fatigue = 0.75^previous_contacts` on `CONTACT_ACTIONS` — repeated nudges decay.
- `subscription_linked` with any non-`WAIT_FOR_NATIVE_RETRY` action: `×0.9` — duplicates Razorpay's native retry effort.

These numbers are **not empirical** — they encode the taxonomy intuition from `ARCHITECTURE.md`. Change them and both training and evaluation shift together.

---

## Model and features

- **Family:** `HistGradientBoostingClassifier` (`app/ml/train.py:53`) — `max_iter=200, learning_rate=0.06, max_depth=5`, trained via `scikit-learn 1.5.2`, with `ColumnTransformer(OneHotEncoder(handle_unknown=ignore))` on categoricals and integers/doubles passed through.
- **Features:** `["failure_category", "action_taken"]` (categorical) + `["amount", "days_overdue", "previous_contacts", "subscription_linked", "hour", "day_of_week"]` (numeric) — `train.py:40`.
- **Target:** `recovered: bool` (Bernoulli outcome of `ground_truth.sample_outcome`).
- **Train / holdout:** `train_test_split(..., test_size=0.2, random_state=seed, stratify=y)` (`train.py:64`). Default `n=30000` → `n_train=24000`, `n_test=6000`.

---

## Current approximate metrics

Reported by `python -m app.ml.train` (holdout), with `n=30000, seed=42` on `scikit-learn 1.5.2` on Windows + `HistGradientBoosting` parallel histogram reduction:

```
Trained on 24000 rows, evaluated on 6000 holdout rows (base recovery rate: ~37%)
  ROC-AUC:   0.7756
  Log loss:  0.4715
  Accuracy:  0.7510 (at 0.5 threshold)
```

> Do not treat these numbers as a product claim. They measure how well the model reconstructs the **simulator's assumptions**, not how well it generalizes to real Razorpay traffic. The canonical run to cite is `python -m app.ml.train` on your own machine — variability of a small fraction of a percent between two separate training runs with the same `random_state` is expected (parallel histogram reduction order; see `README.md` Phase 7 scope).

ROC-AUC is **not** the business metric. It answers "does the model order `P(recovery)` well", not "does picking the highest `P·amount − cost` action recover more net revenue at acceptable outreach". The experiment runner is the business-level benchmark (next section).

---

## Expected-value scorer `app/ml/scorer.py`

For each guardrail-allowed action:

```
expected_value(action) = P̂(recovery | context, action) · amount − ACTION_COST[action]
```

`ACTION_COST` (`app/ml/costs.py:15`):

| Action | INR |
|--------|-----|
| `WAIT` | 0 |
| `WAIT_FOR_NATIVE_RETRY` | 0 |
| `CREATE_PAYMENT_LINK` | 5 |
| `CONTACT_CUSTOMER` | 15 |
| `COLLECT_PROMISE_TO_PAY` | 15 |
| `ESCALATE` | 50 |
| `STOP` | 0 |

These are deliberately simple proxies (goodwill/human time), not empirically calibrated friction values. Because `P·amount` (hundreds to tens of thousands) dominates `cost` (5–50) at normal invoice sizes, this objective is primarily a recovered-value ranker, not a true bi-objective revenue/friction tradeoff.

Scorer details:

- `predict_recovery_probability` returns `0.0` for `STOP` without consulting the model.
- `rank_actions(context, allowed_actions)` batch-predicts all `allowed \ {STOP}` rows in one `pipeline.predict_proba` call, then sorts descending by `expected_value` (`scorer.py:116`).
- Model is **cached by `stat(mtime_ns, size)`**; `reset_cache()` in tests.

---

## Baseline policy vs adaptive — experiment methodology

Two interchangeable surfaces (`app/services/experiment_runner.py` and `scripts/evaluate_policies.py`) implement the **same** matched-scenario design:

1. For `count` scenarios, draw `{failure_category, amount}` from weighted profiles + `AMOUNTS = [500, 1500, 4999, 9000, 15000, 25000, 40000, 75000]` (same mix as `run_synthetic_batch.py`, so Phase 3 / Phase 5 evaluations are population-comparable).
2. For each scenario, instantiate **two identical** `RevenueCase(..., state=DIAGNOSED)` rows (one per arm), run `policy_engine.decide` (baseline) and `ml_policy.decide_ml` (adaptive) on each at a **fixed** `evaluation_time = 2026-01-07 12:00` — so guardrails/costs see identical clocks.
3. Sample outcomes from `ground_truth.sample_outcome` using **common random numbers** (`experiment_runner.py:69` `outcome_cache: dict[str,bool]`): when both policies choose the **same** action for a scenario, they receive the **identical** Bernoulli draw from the shared `random.Random(seed)` rather than two independent draws. Without this, arms that agree on most actions still show "difference" that is pure sampling noise — only cases of **policy disagreement** move the comparison.
4. Aggregate per-arm: `amount_at_risk`, `amount_recovered`, `recovery_rate`, `contacts`, `escalations`, `action_cost_proxy` (sum of `ACTION_COST`), `realized_net_value = recovered − action_cost_proxy`, `action_distribution`.

The persisted surface additionally stores `ExperimentCase` rows and exposes `GET /experiments/{run_id}/export.csv`; the script is an ephemeral drop-in for CI and local one-offs.

---

## Current performance characteristics

From a **1000-scenario** matched synthetic evaluation (e.g., `python scripts/evaluate_policies.py --count 1000 --seed 11`), run against the artifact produced by `python -m app.ml.train --n 30000 --seed 42`:

```
                        Baseline                  Adaptive
Revenue at risk (INR)   20,782,876                20,782,876
Revenue recovered (INR)  8,638,936                 8,925,930
Recovery rate                41.6%                     42.9%
Contact actions              253                       474
Escalations                  259                       256
```

> The adaptive policy **increases synthetic recovery** (~+1.3 pp) while **substantially increasing contact-type selections** (×~1.9). Contact count here means **action-selection count** (`CONTACT_CUSTOMER`/`CREATE_PAYMENT_LINK`/`COLLECT_PROMISE_TO_PAY`), **not** delivered messages — delivery does not exist. Escalations are essentially flat. This tradeoff must not be hidden. See `docs/CURRENT_STATE.md` capability matrix notes.

Interpretation (honest):

- This run **validates code and assumptions**, not production lift.
- The gain comes from the adaptive policy more aggressively choosing `CONTACT_CUSTOMER`/`CREATE_PAYMENT_LINK` where the simulator says waiting is poor (especially auth/invalid-instrument), while keeping `ESCALATE` roughly flat.
- Real contact has **real friction** not represented by an `INR 15` proxy — merchant research would be needed to set a defensible tradeoff weight.
- `realized_net_value = recovered − action_cost_proxy` prefers the adaptive policy in this regime because `cost` is small — a trivial consequence of the cost proxies, not evidence of a solved multi-objective.

---

## Circularity / limitations — read critically

1. **Shared simulator for train and evaluate.** Training labels and benchmark outcomes come from the **same** `ground_truth.sample_outcome`. The model is evaluated on data drawn from the distribution it was trained to reconstruct. This is circular by construction — it is the correct way to verify that code + scoring beat a fixed policy **under assumptions**, and the correct way to produce a misleading production claim if presented without that qualification.

2. **Action-uniform training data.** `synthetic_history` samples `action_taken` uniformly across `ALL_ACTIONS` to ensure all `(category, action)` pairs are represented. Real post-deployment data would be **policy-biased** (mostly baseline choices at first) — a classic offline RL / counterfactual evaluation concern.

3. **Restricted context distribution in evaluation.** The experiment draws `days_overdue=0, previous_contacts=0, subscription_linked ∈ {isolated, <8%}`; training draws `days_overdue`/`previous_contacts` from broader distributions but still synthetically. Models that memorize "low-amount recoveries are easier" from simulator-modulated labels inherit that shape directly.

4. **No calibration obligation.** The scorer is not calibrated or validated before ranking — expected-value ranking requires good **rank ordering** more than calibrated probabilities, but mis-calibration at scale still causes bad decisions.

---

## Model artifact lifecycle

- Artifact: `backend/app/ml/artifacts/model.joblib` — `{"pipeline": Pipeline(ColumnTransformer + HistGBClassifier), "feature_columns": …, "seed": …}` (`train.py:85`).
- Gitignored (`backend/.gitignore:8`), reproducible by retraining. Fresh clones have no model until `python -m app.ml.train` is run. CI (`ci.yml`) trains from scratch before `evaluate_policies.py`.
- `scorer._load` validates existence and bundle shape; missing → `ModelNotTrainedError` → `503` on experiments, `sys.exit(1)` on the script.
- Retraining is deterministic modulo parallel histogram variance; re-running an experiment against the **same** `model.joblib` is exactly reproducible for a given `seed`.

---

## What would be required for real model validation?

None of these are implemented in this pass — this is a roadmap for post-deployment correctness.

| Requirement | Why | Concrete next step |
|-------------|-----|--------------------|
| Observed production outcomes | Ground-truth simulator is not reality | Log every `(context, action, recovered, recovered_at, hours_to_recovery)` append-only alongside `RevenueCase`; make it immutable like `AuditEvent` |
| Train/validation split that is **temporal**, not random | Future must not leak into the past | Time-ordered split: `train < cutoff < test` by `case.created_at`; respect merchant-level grouping |
| Calibration | `P̂·amount` ranking with mis-calibrated `P̂` leaves money on the table | Platt/isotonic calibration; report calibration curves and ECE, not just ROC-AUC |
| Counterfactual limitation acknowledgment | Only the chosen action's outcome is ever observed | Use IPS/DR / counterfactual policy evaluation with explicit exploration (ε-greedy or logged-propensity) once data is policy-biased |
| Offline policy evaluation (OPE) before live | Can't A/B an untested policy on live money | OPE on held-out logged traffic; guardrail-constrained second evaluation; fixed random seeds |
| Randomized controlled rollout | Only experiment proves lift | Feature-flag gated ramp: control = baseline policy path, treatment = `ml_policy.decide_ml` path (see `ARCHITECTURE.md` future components) |
| Friction-aware objective | `INR 15` is not customer goodwill | Merchant research + explicit multi-objective/constraint (e.g., contact-rate cap) with calibrated `ACTION_COST` or constrained optimization |

Until then, cite metrics only as **"synthetic simulation benchmark with manually observed parameters"** and always alongside the contact-count tradeoff — see `docs/CURRENT_STATE.md` Runtime reality and `docs/INTEGRATIONS.md` Razorpay Test Mode note.

