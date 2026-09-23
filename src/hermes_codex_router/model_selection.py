from __future__ import annotations

import re
from typing import Any, Iterable


class ModelSelectionError(ValueError):
    pass


def is_openai_model(model_id: str) -> bool:
    """Conservative names for the native OpenAI route, not the proxy's union catalog."""
    return (
        model_id.startswith(("gpt-", "chatgpt-"))
        or re.match(r"^o[1-9][0-9]*(?:-|$)", model_id) is not None
    )


def available_openai_models(
    models: Iterable[dict[str, Any]], *, model_provider: str | None = None
) -> dict[str, tuple[str, ...]]:
    """Accept OpenAI IDs only, even when a proxy route exposes a union catalog."""
    return available_models(
        item
        for item in models
        if isinstance(item.get("id"), str)
        and is_openai_model(item["id"])
        and item.get("modelProvider", item.get("providerID", "openai"))
        in ("openai", model_provider)
    )


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
