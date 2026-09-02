"""
Train the recovery-probability model (Phase 5).

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
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./ml_train.db")

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, log_loss, accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from app.ml.synthetic_history import generate_history

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "model.joblib"

CATEGORICAL_FEATURES = ["failure_category", "action_taken"]
NUMERIC_FEATURES = ["amount", "days_overdue", "previous_contacts", "subscription_linked", "hour", "day_of_week"]
FEATURE_COLUMNS = CATEGORICAL_FEATURES + NUMERIC_FEATURES
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
        "base_rate": float(y.mean()),
    }

    if save:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"pipeline": pipeline, "feature_columns": FEATURE_COLUMNS, "seed": seed}, save_path)
        metrics["saved_to"] = str(save_path)

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
    print(f"  Accuracy:  {metrics['accuracy']:.4f}  (at 0.5 threshold)")
    print(f"\nSaved model to {metrics['saved_to']}")
