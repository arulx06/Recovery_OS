"""
Expected-value action scorer (Phase 5).

    expected_value(action) = P(recovery | context, action) * amount - cost(action)

This is the "interesting ML problem" ARCHITECTURE.md describes: not just
predicting a probability, but turning that probability into a ranked
choice among the actions the guardrail engine still allows. The model
never picks an action directly — it scores whatever candidate set it's
given, and something else (ml_policy.py) is responsible for filtering
that set through guardrails first.

The trained pipeline is loaded once per process and cached — training
takes under a second, but there's no reason to pay even that on every
call. Call reset_cache() in tests that swap MODEL_PATH.
"""
from pathlib import Path

import joblib
import pandas as pd

from app.ml.costs import ACTION_COST
from app.ml.train import MODEL_PATH, FEATURE_COLUMNS

_cache: dict | None = None


class ModelNotTrainedError(RuntimeError):
    pass


def reset_cache():
    global _cache
    _cache = None


def _load(model_path: Path = MODEL_PATH) -> dict:
    global _cache
    if _cache is not None and _cache.get("_path") == str(model_path):
        return _cache

    if not model_path.exists():
        raise ModelNotTrainedError(
            f"No trained model at {model_path}. Run `python -m app.ml.train` first."
        )

    bundle = joblib.load(model_path)
    bundle["_path"] = str(model_path)
    _cache = bundle
    return bundle


def predict_recovery_probability(
    category: str, action: str, amount: float,
    days_overdue: float = 0, previous_contacts: int = 0,
    subscription_linked: bool = False, hour: int = 12, day_of_week: int = 2,
    model_path: Path = MODEL_PATH,
) -> float:
    if action == "STOP":
        return 0.0  # by definition — no model call needed, and no training row ever labels STOP as a real attempt

    bundle = _load(model_path)
    pipeline = bundle["pipeline"]

    row = pd.DataFrame([{
        "failure_category": category,
        "action_taken": action,
        "amount": amount,
        "days_overdue": days_overdue,
        "previous_contacts": previous_contacts,
        "subscription_linked": subscription_linked,
        "hour": hour,
        "day_of_week": day_of_week,
    }])[FEATURE_COLUMNS]

    return float(pipeline.predict_proba(row)[0, 1])


def score_action(context: dict, action: str, model_path: Path = MODEL_PATH) -> dict:
    p_recovery = predict_recovery_probability(
        category=context["category"],
        action=action,
        amount=context["amount"],
        days_overdue=context.get("days_overdue", 0),
        previous_contacts=context.get("previous_contacts", 0),
        subscription_linked=context.get("subscription_linked", False),
        hour=context.get("hour", 12),
        day_of_week=context.get("day_of_week", 2),
        model_path=model_path,
    )
    cost = ACTION_COST.get(action, 0.0)
    expected_value = p_recovery * float(context["amount"]) - cost
    return {"action": action, "p_recovery": p_recovery, "cost": cost, "expected_value": expected_value}


def rank_actions(context: dict, allowed_actions: list[str], model_path: Path = MODEL_PATH) -> list[dict]:
    """
    Scores every action in allowed_actions and returns them sorted by
    expected_value, highest first. Doesn't know or care why an action is
    or isn't in allowed_actions — that's the guardrail engine's job.
    """
    scored = [score_action(context, action, model_path=model_path) for action in allowed_actions]
    return sorted(scored, key=lambda s: s["expected_value"], reverse=True)
