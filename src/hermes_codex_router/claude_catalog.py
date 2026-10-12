"""Explicit local Claude choices; no availability, entitlement or billing discovery."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Protocol

from .provider_catalog import DEFAULT_CATALOG_TTL, ProviderCatalogError, ProviderModel
from .provider_catalog_cache import CatalogSnapshot, ProviderCatalogCache

CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CONFIGURED_SOURCE = "configured Claude choices; availability unverified"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}")


class ClaudeCatalogAgent(Protocol):
    @property
    def agent_id(self) -> str: ...

    @property
    def default_model(self) -> str: ...

    @property
    def default_effort(self) -> str: ...

    @property
    def model_catalog(self) -> tuple[ProviderModel, ...] | None: ...


def parse_claude_catalog(
    value: object, *, default_model: str, default_effort: str
) -> tuple[ProviderModel, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("model_catalog must contain 1–32 configured Claude choices")
    models: list[ProviderModel] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"model_id", "label", "efforts"}:
            raise ValueError("model_catalog entries require model_id, label and efforts only")
        model_id, label, efforts = row["model_id"], row["label"], row["efforts"]
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id) or model_id in seen:
            raise ValueError("model_catalog model_id must be unique and bounded ASCII")
        if (
            not isinstance(label, str)
            or not 1 <= len(label) <= 96
            or not label.isprintable()
            or label != label.strip()
        ):
            raise ValueError("model_catalog labels must contain 1–96 printable characters")
        if (
            not isinstance(efforts, list)
            or not 1 <= len(efforts) <= len(CLAUDE_EFFORTS)
            or not all(isinstance(effort, str) and effort in CLAUDE_EFFORTS for effort in efforts)
            or len(set(efforts)) != len(efforts)
        ):
            raise ValueError("model_catalog efforts must be a unique supported Claude subset")
        seen.add(model_id)
        models.append(ProviderModel(model_id, label, tuple(efforts)))
    if not any(
        model.model_id == default_model and default_effort in model.efforts for model in models
    ):
        raise ValueError("model_catalog must include the configured default model and effort")
    return tuple(models)


def configured_claude_models(agent: ClaudeCatalogAgent) -> tuple[ProviderModel, ...]:
    return agent.model_catalog or (
        ProviderModel(agent.default_model, agent.default_model, (agent.default_effort,)),
    )


def configured_claude_snapshot(
    cache: ProviderCatalogCache,
    agent: ClaudeCatalogAgent,
    *,
    refresh: bool = False,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_CATALOG_TTL,
) -> CatalogSnapshot:
    models = configured_claude_models(agent)
    before = cache.load(agent.agent_id)
    if (
        not refresh
        and before is not None
        and before.source_version == CONFIGURED_SOURCE
        and tuple((model.model_id, model.label, model.efforts) for model in before.models)
        == tuple((model.model_id, model.label, model.efforts) for model in models)
        and not cache.is_stale(agent.agent_id, max_age=max_age, now=now)
    ):
        return before
    snapshot = cache.store(
        agent.agent_id, models, source_version=CONFIGURED_SOURCE, observed_at=now
    )
    # Store reloads the shared file. A monitor with older configuration may
    # replace it between the write and read; that must never authorize a choice.
    if (
        snapshot.agent_id != agent.agent_id
        or snapshot.source_version != CONFIGURED_SOURCE
        or tuple((model.model_id, model.label, model.efforts) for model in snapshot.models)
        != tuple((model.model_id, model.label, model.efforts) for model in models)
    ):
        raise ProviderCatalogError("configured Claude choices changed during cache reconciliation")
    return snapshot
