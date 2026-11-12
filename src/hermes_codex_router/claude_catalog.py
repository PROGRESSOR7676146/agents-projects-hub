"""Claude choices: explicit configuration, else the CPA route's model metadata.

Neither proves availability, entitlement or billing. Route discovery reads only
the loopback proxy's model list (no inference) and keeps Anthropic-owned models.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .provider_catalog import DEFAULT_CATALOG_TTL, ProviderCatalogError, ProviderModel
from .provider_catalog_cache import CatalogSnapshot, ProviderCatalogCache

CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
CONFIGURED_SOURCE = "configured Claude choices; availability unverified"
ROUTE_SOURCE = "Claude route model list; availability unverified"
_ROUTE_MODEL = re.compile(r"claude-(fable|opus|sonnet|haiku)-([0-9]+)(?:-([0-9]))?(?:-[0-9]{8})?")
_FAMILIES = ("fable", "opus", "sonnet", "haiku")
_MAX_ROUTE_BYTES = 1024 * 1024
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


def route_claude_models(
    rows: object, *, default_model: str, default_effort: str
) -> tuple[ProviderModel, ...]:
    """Newest Anthropic model per family from a proxy model list, plus the default.

    Proxy aliases owned by other providers are never offered as Claude choices.
    """
    newest: dict[str, tuple[tuple[int, int], str, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("owned_by") != "anthropic":
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        match = _ROUTE_MODEL.fullmatch(model_id)
        if match is None:
            continue
        family, version = match[1], (int(match[2]), int(match[3] or 0))
        display = row.get("display_name")
        label = display.strip() if isinstance(display, str) and display.strip() else model_id
        if len(label) > 96 or not label.isprintable():
            label = model_id
        if family not in newest or version > newest[family][0]:
            newest[family] = (version, model_id, label)
    models: list[ProviderModel] = []
    for family in _FAMILIES:
        if family in newest:
            _, model_id, label = newest[family]
            efforts = (
                (default_effort,)
                if family == "haiku"
                else tuple(sorted(CLAUDE_EFFORTS, key=_EFFORT_ORDER.index))
            )
            models.append(ProviderModel(model_id, label, efforts))
    if not any(model.model_id == default_model for model in models):
        models.append(ProviderModel(default_model, default_model, (default_effort,)))
    return tuple(models)


def fetch_route_model_rows(route_environment: Path) -> list[object]:
    """Read the loopback proxy's model list; metadata only, never inference."""
    from .claude_local import read_route_environment

    route = read_route_environment(route_environment)
    request = urllib.request.Request(
        route["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1/models",
        headers={
            "Authorization": "Bearer " + route["ANTHROPIC_AUTH_TOKEN"],
            "anthropic-version": "2023-06-01",
        },
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            raise ProviderCatalogError("Claude route model list redirected")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    with opener.open(request, timeout=10) as response:
        body = response.read(_MAX_ROUTE_BYTES + 1)
    if len(body) > _MAX_ROUTE_BYTES:
        raise ProviderCatalogError("Claude route model list is too large")
    document = json.loads(body)
    rows = document.get("data") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ProviderCatalogError("Claude route model list is malformed")
    return rows


def route_claude_snapshot(
    cache: ProviderCatalogCache,
    agent: ClaudeCatalogAgent,
    rows: Sequence[object],
    *,
    now: datetime | None = None,
) -> CatalogSnapshot:
    models = route_claude_models(
        rows, default_model=agent.default_model, default_effort=agent.default_effort
    )
    return cache.store(agent.agent_id, models, source_version=ROUTE_SOURCE, observed_at=now)


def cached_claude_snapshot(
    cache: ProviderCatalogCache, agent: ClaudeCatalogAgent
) -> CatalogSnapshot | None:
    """A saved route catalog that still offers the configured default, else none."""
    if agent.model_catalog:
        return None
    snapshot = cache.load(agent.agent_id)
    if (
        snapshot is None
        or snapshot.source_version != ROUTE_SOURCE
        or not any(
            model.model_id == agent.default_model and agent.default_effort in model.efforts
            for model in snapshot.models
        )
    ):
        return None
    return snapshot
