from __future__ import annotations

from typing import Any, Iterable


class ModelSelectionError(ValueError):
    pass


def available_models(models: Iterable[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    available: dict[str, tuple[str, ...]] = {}
    for item in models:
        model_id = item.get("id")
        raw_efforts = item.get("supportedReasoningEfforts")
        if not isinstance(model_id, str) or not isinstance(raw_efforts, list):
            continue
        efforts = tuple(
            effort["reasoningEffort"]
            for effort in raw_efforts
            if isinstance(effort, dict) and isinstance(effort.get("reasoningEffort"), str)
        )
        if efforts:
            available[model_id] = efforts
    return available


def require_model_effort(
    models: Iterable[dict[str, Any]], model: str, effort: str
) -> tuple[str, str]:
    available = available_models(models)
    if model not in available:
        raise ModelSelectionError(f"model is unavailable: {model}")
    if effort not in available[model]:
        raise ModelSelectionError(f"effort {effort} is unavailable for {model}")
    return model, effort
