"""
Model manifest and fingerprint — provenance for adaptive decisions.
"""

import hashlib
import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from app.core.time import utc_now

MANIFEST_VERSION = "1"
MODEL_VERSION = "recovery-v1"


def compute_fingerprint(model_path: Path) -> str:
    """SHA-256 of model.joblib bytes, hex, short 8-char also useful."""
    h = hashlib.sha256()
    with open(model_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(
    model_path: Path,
    feature_columns: list[str],
    feature_schema_version: str,
    training_seed: int,
    training_n: int,
    holdout_n: int,
    metrics: dict[str, Any],
    action_set: list[str],
) -> dict:
    fingerprint = compute_fingerprint(model_path) if model_path.exists() else None
    return {
        "manifest_version": MANIFEST_VERSION,
        "model_version": MODEL_VERSION,
        "artifact_format_version": MANIFEST_VERSION,
        "feature_schema_version": feature_schema_version,
        "feature_names": list(feature_columns),
        "action_set": sorted(action_set),
        "trained_at": utc_now().isoformat(),
        "training_seed": training_seed,
        "training_n": training_n,
        "holdout_n": holdout_n,
        "metrics": metrics,
        "model_class": "HistGradientBoostingClassifier",
        "fingerprint": fingerprint,
        "fingerprint_short": fingerprint[:8] if fingerprint else None,
        "synthetic_data_notice": "synthetic simulation benchmark — not production lift",
        "library_versions": _library_versions(),
    }


def _library_versions() -> dict:
    versions = {}
    try:
        import sklearn
        versions["scikit-learn"] = sklearn.__version__
    except Exception:
        pass
    try:
        import pandas
        versions["pandas"] = pandas.__version__
    except Exception:
        pass
    try:
        import joblib
        versions["joblib"] = joblib.__version__
    except Exception:
        pass
    return versions


def write_manifest(manifest_path: Path, manifest: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def load_manifest(manifest_path: Path) -> dict | None:
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return None


def validate_manifest(
    manifest: dict | None,
    expected_feature_schema: str,
    expected_action_set: list[str],
) -> tuple[bool, str]:
    """Return (compatible, reason)."""
    if manifest is None:
        return False, "manifest missing"
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return False, f"manifest_version mismatch: {manifest.get('manifest_version')} vs {MANIFEST_VERSION}"
    if manifest.get("feature_schema_version") != expected_feature_schema:
        return False, f"feature_schema mismatch: {manifest.get('feature_schema_version')} vs {expected_feature_schema}"
    if sorted(manifest.get("action_set", [])) != sorted(expected_action_set):
        return False, "action_set mismatch"
    if not manifest.get("fingerprint"):
        return False, "fingerprint missing"
    return True, "ok"
