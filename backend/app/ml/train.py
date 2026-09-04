"""
Train the recovery-probability model.

    P(recovered | failure_category, action, amount, days_overdue,
                  previous_contacts, subscription_linked, hour, day_of_week)

Deliberately not a neural network and not reinforcement learning — per
ARCHITECTURE.md, a gradient-boosted tree on tabular features is easy to
validate, easy to explain, and more than enough for this feature set.
HistGradientBoostingClassifier specifically because it handles the mixed
categorical/numeric features here without a separate encoding step for
the numeric ones, and it's fast enough to retrain from scratch in well
under a second on a few thousand rows.

Usage:
    python -m app.ml.train [--n 3000] [--seed 42]

Writes the fitted pipeline to app/ml/artifacts/model.joblib and prints
holdout metrics — this is the reproducible part of "offline evaluation
reproducible from repo": anyone can re-run this and get the same numbers
(same seed, same synthetic generator, same model).
"""
import argparse
from pathlib import Path

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score, accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from app.ml.synthetic_history import generate_history
from app.ml.features import FEATURE_COLUMNS as CANONICAL_FEATURE_COLUMNS, FEATURE_SCHEMA_VERSION
from app.ml.manifest import build_manifest, write_manifest, MODEL_VERSION

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "model.joblib"
MANIFEST_PATH = ARTIFACT_DIR / "manifest.json"

# Keep local aliases for backward compat but canonical is features.py
CATEGORICAL_FEATURES = ["failure_category", "action_taken"]
NUMERIC_FEATURES = ["amount", "days_overdue", "previous_contacts", "subscription_linked", "hour", "day_of_week"]
FEATURE_COLUMNS = CANONICAL_FEATURE_COLUMNS
TARGET_COLUMN = "recovered"


def build_pipeline(random_state: int) -> Pipeline:
    preprocessor = ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
        ],
        remainder="passthrough",  # numeric features pass through unchanged
    )
    model = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.06, max_depth=5, random_state=random_state,
    )
    return Pipeline([("preprocess", preprocessor), ("model", model)])


def train(n: int = 30000, seed: int = 42, save: bool = True, save_path: Path = MODEL_PATH) -> dict:
    df = generate_history(n=n, seed=seed)
    X = df[FEATURE_COLUMNS]
    y = df[TARGET_COLUMN].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=seed, stratify=y,
    )

    pipeline = build_pipeline(random_state=seed)
    pipeline.fit(X_train, y_train)

    proba = pipeline.predict_proba(X_test)[:, 1]
    preds = (proba >= 0.5).astype(int)

    metrics = {
        "n_train": len(X_train),
        "n_test": len(X_test),
        "roc_auc": float(roc_auc_score(y_test, proba)),
        "log_loss": float(log_loss(y_test, proba)),
        "accuracy": float(accuracy_score(y_test, preds)),
        "brier_score": float(brier_score_loss(y_test, proba)),
        "base_rate": float(y_test.mean()),
    }

    if save:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        # Include versioned bundle for compatibility checks
        bundle = {
            "pipeline": pipeline,
            "feature_columns": FEATURE_COLUMNS,
            "seed": seed,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "model_version": MODEL_VERSION,
        }
        joblib.dump(bundle, save_path)
        metrics["saved_to"] = str(save_path)
        # Manifest beside artifact — provenance, not secrets
        from app.services.policy_engine import ALL_ACTIONS
        manifest = build_manifest(
            model_path=save_path,
            feature_columns=FEATURE_COLUMNS,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            training_seed=seed,
            training_n=len(X_train),
            holdout_n=len(X_test),
            metrics={k: v for k, v in metrics.items() if k not in ("saved_to",)},
            action_set=ALL_ACTIONS,
        )
        write_manifest(MANIFEST_PATH, manifest)
        metrics["manifest"] = str(MANIFEST_PATH)
        metrics["fingerprint"] = manifest.get("fingerprint_short")

    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=30000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    metrics = train(n=args.n, seed=args.seed)

    print(f"Trained on {metrics['n_train']} rows, evaluated on {metrics['n_test']} holdout rows "
          f"(base recovery rate in holdout: {metrics['base_rate']:.1%})\n")
    print(f"  ROC-AUC:   {metrics['roc_auc']:.4f}")
    print(f"  Log loss:  {metrics['log_loss']:.4f}")
    print(f"  Brier:     {metrics['brier_score']:.4f}")
    print(f"  Accuracy:  {metrics['accuracy']:.4f}  (at 0.5 threshold)")
    # Calibration note: Brier ~0.18-0.20 is reasonable for this synthetic task;
    # no Platt/isotonic calibration applied — synthetic holdout size and
    # histogram GBDT's native probability ranking are sufficient for utility ordering.
    if "fingerprint" in metrics:
        print(f"  Fingerprint: {metrics['fingerprint']}  (model {MODEL_VERSION}, schema {FEATURE_SCHEMA_VERSION})")
    print(f"\nSaved model to {metrics['saved_to']}")
    if "manifest" in metrics:
        print(f"Saved manifest to {metrics['manifest']}")
