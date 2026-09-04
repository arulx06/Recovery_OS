# RecoveryOS — ML and Evaluation

> Live friction-aware adaptive is now the default experiment comparator, but training and evaluation remain **synthetic**. This file is the honest provenance for features, model, utility, and the revenue-vs-friction tradeoff.

---

## Training data origin

- **No real Razorpay outcome data exists yet.** Until RecoveryOS observes production `recovered` outcomes, there are no `{context, action, outcome}` labels from real traffic.
- The sole source of training and evaluation labels is the hand-authored simulator `app/ml/ground_truth.py` (see `ARCHITECTURE.md: ML boundary`).
- When real append-only outcome logs exist, `synthetic_history.generate_history` is the module replaced — `train.py` accepts any `DataFrame` with `FEATURE_COLUMNS`.

---

## Synthetic generator `app/ml/synthetic_history.py`

```python
generate_history(n=30000, seed=42) → DataFrame[case_id, failure_category, action_taken,
                                               amount, days_overdue, previous_contacts,
                                               subscription_linked, hour, day_of_week, recovered]
```

- **Failure category**: weighted draw from `CATEGORY_WEIGHTS` (balance 22, transient 16, auth 14, invalid instrument 12, …).
- **`action_taken`**: **uniform** across `ALL_ACTIONS` (not baseline's top pick) — exposes the model to all `(category, action)` pairs.
- **Amount**: uniform over `[500, 1000, 2500, 5000, 9999, 15000, 25000, 40000, 75000, 150000]`.
- **`days_overdue` / `previous_contacts`**: skewed toward small values; `subscription_linked` enriched for `SUBSCRIPTION_PENDING_NATIVE_RETRY`.
- **`recovered`**: `ground_truth.sample_outcome(..., rng=Random(seed))` — one Bernoulli draw.

### Ground-truth assumptions `app/ml/ground_truth.py:33`

Same table as before (e.g., `TRANSIENT,WAIT 0.78`, `INVALID_INSTRUMENT,CREATE 0.65`, etc.) plus deterministic modifiers `amount_factor`, `overdue_factor`, `0.75**previous_contacts`, `subscription_linked` penalty. **Not empirical** — encode taxonomy intuition. Change them and both training and evaluation shift together. See file for full table.

---

## Live-feature validity audit

Before wiring adaptive into `orchestrator`, every feature was classified:

| Feature | Status | Source at decision time | Reason |
|---------|--------|--------------------------|--------|
| `failure_category` | **LIVE_AVAILABLE** | `RevenueCase.failure_category` from `failure_diagnosis.classify_failure` | Known after diagnosis |
| `action_taken` | **ACTION-CONDITIONAL** | Candidate action being scored (not a case property) | Model is `P(recovery \| context, action)`; each candidate scored with same context + that action |
| `amount` | **LIVE_AVAILABLE** | `RevenueCase.amount` | Direct case field |
| `days_overdue` | **DERIVABLE_FROM_HISTORY** | `(now - case.created_at)/86400`, bounded | Decision clock minus case creation; no future info |
| `previous_contacts` | **DERIVABLE_FROM_HISTORY** | `len(policy_engine.contacts_for_case(db, case))`, bounded query (`SCHEDULED`/`EXECUTED` actions) | Count of prior contact-type actions on this case only; no unbounded scan |
| `subscription_linked` | **LIVE_AVAILABLE** | `bool(case.razorpay_subscription_id)` | Direct case field |
| `hour` | **LIVE_AVAILABLE** | `now.hour` (decision clock, merchant-agnostic) | Deterministic; same distribution as synthetic `randint(0,23)` but live-derived |
| `day_of_week` | **LIVE_AVAILABLE** | `now.weekday()` | Same |

**No leakage:** `recovered`, `payment.captured` success, `amount_paid`, `future PTP`, or simulator latent state is available at `DIAGNOSED` decision time — none is used. No post-action observation is fed back as feature.

---

## One canonical feature pipeline

`app/ml/features.py` is the single source:

```python
FEATURE_COLUMNS = ["failure_category","action_taken","amount","days_overdue",
                   "previous_contacts","subscription_linked","hour","day_of_week"]
FEATURE_SCHEMA_VERSION = "v1"

def build_live_features(db, case, action, now) -> dict:
    # previous_contacts via contacts_for_case (bounded)
    # days_overdue via (now - created_at)
    # ... same semantics as synthetic_history row
def validate_feature_row(row: dict): ...
```

- `train.py` imports `FEATURE_COLUMNS`/`FEATURE_SCHEMA_VERSION` from `features.py` — training DataFrame is exactly `FEATURE_COLUMNS`.
- `scorer.py` imports the same `FEATURE_COLUMNS` and calls `validate_feature_row` on every live row before `predict_proba`; mismatched `feature_columns` in an old `model.joblib` bundle raises `ModelNotTrainedError` (fingerprint check).
- Model feature order is explicit and validated; `OneHotEncoder(handle_unknown="ignore")` for categoricals, passthrough for numerics — same `ColumnTransformer` in train and serve.
- **Parity test:** `tests/test_adaptive_policy.py::test_train_serve_parity` asserts `train.FEATURE_COLUMNS == features.FEATURE_COLUMNS == scorer.FEATURE_COLUMNS` and that a synthetic training row round-trips through `build_live_features`.

---

## Model and provenance

- **Family:** `HistGradientBoostingClassifier` (`max_iter=200, lr=0.06, max_depth=5`, `random_state=seed`) — `train.py:46`. Chosen because it handles mixed categorical/numeric without separate encoding, is fast (<1s for 30k rows), and its native probability ranking is sufficient for utility ordering.
- **Target:** `recovered: bool` (`ground_truth.sample_outcome`).
- **Split:** `train_test_split(..., test_size=0.2, random_state=seed, stratify=y)` — default `n=30000` → `24000/6000`. No grouping needed for synthetic i.i.d. rows; real data will need temporal/merchant grouping (see roadmap).
- **Artifact:** `backend/app/ml/artifacts/model.joblib` = `{"pipeline": Pipeline, "feature_columns": FEATURE_COLUMNS, "seed": seed, "feature_schema_version": "v1", "model_version": "recovery-v1"}`. Gitignored, reproducible.
- **Manifest:** `backend/app/ml/artifacts/manifest.json` written atomically beside the joblib on every `train`:
  ```json
  {
    "manifest_version": "1",
    "model_version": "recovery-v1",
    "feature_schema_version": "v1",
    "feature_names": ["failure_category", ...],
    "action_set": ["COLLECT_PROMISE_TO_PAY", ..., "WAIT"],
    "trained_at": "2026-09-04T...",
    "training_seed": 42,
    "training_n": 24000,
    "holdout_n": 6000,
    "metrics": {"roc_auc": "<generated>", "log_loss": "<generated>", "brier_score": "<generated>", "accuracy": "<generated>", "base_rate": "<generated>"},
    "model_class": "HistGradientBoostingClassifier",
    "fingerprint": "<sha256 of this generated joblib>",
    "fingerprint_short": "<first 8 hex characters>",
    "synthetic_data_notice": "synthetic simulation benchmark — not production lift",
    "library_versions": {"scikit-learn": "1.5.2", ...}
  }
  ```
  Fingerprint is `sha256(model.joblib)` — short `[:8]` is used in `Decision`/`AuditEvent` provenance. Manifest is not required to be committed; versioned `app/ml/features.py` and `train.py` are the source of truth.
- **Loading:** `scorer._load` validates `feature_columns` set equality, manifest `feature_schema_version` and `action_set`, runs a smoke test (`UNKNOWN/WAIT` row → `0≤p≤1`), and caches by `stat(mtime,size)`. Incompatible artifact → `ModelNotTrainedError` → adaptive falls back to baseline (never `500`), with `adaptive_fallback` audit.

---

## Generated Metrics

Run `python -m app.ml.train` to generate holdout metrics for the current code and dependency versions. The default command uses `n=30000`, `seed=42`, and an 80/20 stratified split.

The command prints ROC-AUC, log loss, Brier score, accuracy, holdout base rate, and the generated artifact fingerprint. The same values are written to `manifest.json`. They are intentionally not copied into this document because generated metrics can change with implementation or dependency updates.

ROC-AUC is **not** the business metric. The experiment runner is.

---

## Friction is a first-class objective

Using only `expected_value = p*amount - ACTION_COST` makes small action-cost values insignificant relative to typical payment amounts and can over-favor customer contact. RecoveryOS therefore models friction separately.

Now:

```python
# app/ml/friction.py
BASE_FRICTION = {
    "WAIT": 0, "WAIT_FOR_NATIVE_RETRY": 2,
    "CREATE_PAYMENT_LINK": 25, "CONTACT_CUSTOMER": 40,
    "COLLECT_PROMISE_TO_PAY": 60, "ESCALATE": 80, "STOP": 0
}
CONTACT_INCREMENT = 12  # soft preference, distinct from hard max_contacts=3 guardrail

def compute_friction_score(action, case, db) -> float:
    base = BASE_FRICTION[action]
    if action in CONTACT_ACTIONS:
        base += len(contacts_for_case(db, case)) * CONTACT_INCREMENT
    return base   # dimensionless, deterministic, no model

PROFILE_WEIGHTS = {
    "revenue_first": 4.0,   # small INR per friction point — tolerates contact
    "balanced": 18.0,       # default — friction matters (40*18=720 INR vs p*amount)
    "low_friction": 45.0,   # strongly restrains
}
utility = p*amount - ACTION_COST - friction_weight * friction_score
```

- `BASE_FRICTION` ordering reflects product semantics (observe < link < outreach < ask-for-commitment < human). `CONTACT_INCREMENT` makes second contact less desirable than first without duplicating the hard `max_contacts_per_case=3` block.
- `friction_weight` is **INR per friction point** — a policy preference, not a measured cost. Dashboards show `recovered INR`, `action_cost INR`, and `friction_score` separately; utility uses the conversion but reports components separately.
- Hard guardrails ( `max_contacts`, `cooldown`, `max_automated_amount`, `STOP` ) remain authoritative and are checked **before** scoring — ML never bypasses them.

---

## Utility and candidate representation

For each guardrail-allowed action (semantic filter additionally removes `WAIT_FOR_NATIVE_RETRY` when `not subscription_linked` and `category != SUBSCRIPTION_PENDING_NATIVE_RETRY`):

```python
scorer.rank_actions_with_friction(context, allowed, friction_scores, weight)
# batched: one predict_proba for all allowed\{STOP}
# returns sorted by utility descending, each entry:
# {action, p_recovery, cost, expected_value, expected_recovered_value,
#  friction_score, friction_weight, utility}
```

`ml_policy._adaptive_choose` builds `alternatives` for the `Decision`:

```json
{
  "CREATE_PAYMENT_LINK": {
    "allowed": true,
    "p_recovery": 0.62, "cost": 5, "expected_value": 3095.0,
    "friction_score": 25, "friction_weight": 18, "utility": 2645.0,
    "expected_recovered_value": 3100
  },
  "WAIT": {"allowed": false, "reason": "semantically infeasible ..."}
}
```

`Decision.expected_value` stores classic `EV` (for backward compat); `Decision.alternatives["_provenance"]` stores `{policy_mode:"adaptive", model_version:"recovery-v1", fingerprint:"<current artifact SHA-256>", friction_profile:"balanced", friction_weight:18, top_utility, top_expected_value}` plus `Decision.policy_mode/model_version/fingerprint/friction_*` first-class columns. `AuditEvent(adaptive_decision)` mirrors it.

---

## Experiment methodology (matched, common-random, RNG-decoupled)

Both surfaces (`experiment_runner.run_experiment` and `scripts/evaluate_policies.py:run`) keep the same matched design but with decoupled RNGs:

1. For `count` scenarios, draw `{failure_category, amount}` from `scenario_rng = Random(seed)` — **only** scenario/profile/amount may consume it. `subscription_linked` from `subscription_id` presence.
2. For each scenario, instantiate **two identical** `RevenueCase(..., state=DIAGNOSED)` (one per arm), run `policy_engine.decide` (baseline) and `ml_policy.decide_ml` (adaptive, friction-aware) at `evaluation_time=2026-01-07 12:00`, guardrails identical. Same `seed`/`count` therefore gives identical baseline scenarios/outcomes regardless of adaptive profile.
3. Sample outcomes **deterministically** and **policy-path-independently**: `p = true_probability(category, action, amount, ...)` and `uniform = int(hashlib.sha256(f"{seed}:{scenario_idx}:{action}".encode()).hexdigest()[:8],16)/0xffffffff`, outcome `uniform < p` cached per `(seed, scenario_idx, action)`. When both arms choose same action they get identical outcome; later scenarios are never perturbed by earlier policy choices.
4. Persist per-scenario/per-arm `ExperimentCase` rows (`contacts_made`, `recovered`, etc.) grouped by `run_id`; also persist one `ExperimentRun` row at run time with `resolved_seed` (even when caller passed `seed=None`), `scenario_count`, `model_version`, `model_fingerprint`, `feature_schema_version`, `adaptive_profile`, `friction_weight`, `created_at`. `ExperimentCase` remains the per-scenario table; `ExperimentRun` is the run-level provenance table.
5. `summarize_run` **must** read historical provenance from `ExperimentRun` (not current `scorer.get_model_info()` / `settings`). `realized_policy_utility = amount_recovered - action_cost_proxy - ExperimentRun.friction_weight * friction_score` (weight `4` for `revenue_first`, `18` for `balanced`, `45` for `low_friction` per run). Synthetic evaluation remains `!=` production lift.

---

## Evaluating Policy Tradeoffs

The profile weights `revenue_first:4`, `balanced:18`, and `low_friction:45` are policy preferences rather than empirically optimal values. Compare them with the same scenario count and seed:

```powershell
$env:ADAPTIVE_POLICY_PROFILE="revenue_first"
python scripts/evaluate_policies.py --count 1000 --seed 11

$env:ADAPTIVE_POLICY_PROFILE="balanced"
python scripts/evaluate_policies.py --count 1000 --seed 11

$env:ADAPTIVE_POLICY_PROFILE="low_friction"
python scripts/evaluate_policies.py --count 1000 --seed 11
```

Repeat with multiple fixed seeds and compare recovered amount, contact rate, escalations, friction score, and realized policy utility together. Do not treat any synthetic difference as expected production lift.

No additional probability calibration is applied. Calibration should be reassessed with reliability diagrams and expected calibration error once real holdout data is available; `CalibratedClassifierCV` can be introduced if the real-data results justify it.

---

## Model artifact lifecycle

- `model.joblib` = `{"pipeline": ..., "feature_columns": FEATURE_COLUMNS, "feature_schema_version": "v1", "model_version": "recovery-v1", ...}` plus `manifest.json` beside it. Both gitignored.
- `scorer._load` validates `feature_columns` set equality, `manifest.feature_schema_version`, `action_set`, and runs a smoke test (`UNKNOWN/WAIT` → `0≤p≤1`, not NaN) — incompatible → `ModelNotTrainedError` → `adaptive_fallback`.
- `get_model_info()` exposes `{available, model_version, feature_schema_version, fingerprint, fingerprint_short, trained_at, metrics}` for `/health` and `Decision` provenance. Tests use `tmp_path_factory` isolated artifacts.

---

## Real-data limitations and roadmap

Synthetic circularity remains: training and evaluation share `ground_truth` — validates code and utility ordering, not production lift. Real validation requires:

| Requirement | Next step |
|-------------|-----------|
| Observed outcomes | Append-only `(decision_time_features, action, policy_version, recovered, recovered_at, amount_recovered)` — extend `Decision` provenance already captured (`alternatives[_provenance]` + `AuditEvent`) plus outcome timestamp. |
| Temporal split | `train < cutoff < test` by `case.created_at`; merchant grouping. |
| Calibration on real holdout | ECE + reliability diagram; `CalibratedClassifierCV` if needed. |
| Counterfactual gap | Only chosen action's outcome is observed — need `IPS`/`DR` with ε-greedy or logged `p` (shadow already logs `candidates` + `fingerprint` for propensities). |
| Offline policy evaluation | Guardrail-constrained second evaluation on logged propensities before ramp. |
| Controlled rollout | `RECOVERY_POLICY` global-at-decision-time ramp (`baseline` → `shadow` → `adaptive` with `low_friction` → `balanced`), `Decision.policy_mode` as bucket. |

Training remains explicit/offline — no online retraining. Friction weights are policy preferences to be set with merchant research, not tuned until a benchmark looks good.
