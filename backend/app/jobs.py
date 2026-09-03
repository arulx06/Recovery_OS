"""Importable RQ job functions."""
from app.services.temporal_runtime import process_action as _process_action


def process_action(action_id: str) -> str:
    return _process_action(action_id)
