from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Callable

from .hub_config import HubConfig
from .provider_catalog import (
    DEFAULT_CATALOG_TTL,
    ProviderCatalogError,
    ProviderModel,
    antigravity_models,
    codex_models,
    opencode_models,
)
from .provider_catalog_cache import ProviderCatalogCache

Run = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    refreshed: tuple[str, ...]
    failed: tuple[str, ...]
    added: dict[str, tuple[str, ...]]


def _source_version(executable: str, *, run: Run) -> str | None:
    try:
        result = run(
            (executable, "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0][:128] if lines else None


def refresh_provider_catalogs(
    config: HubConfig,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_CATALOG_TTL,
    run: Run = subprocess.run,
) -> CatalogRefreshResult:
    """Refresh stale catalogs from deterministic provider CLIs.

    This belongs to the monitor plane, never to Telegram command handling. A
    failed refresh preserves the last known-good snapshot.
    """
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    cache = ProviderCatalogCache(config.state_path.with_name("provider-model-catalogs.json"))
    refreshed: list[str] = []
    failed: list[str] = []
    added: dict[str, tuple[str, ...]] = {}

    for agent in config.agents:
        if agent.managed_externally or agent.runtime not in {"codex", "opencode", "antigravity"}:
            continue
        if not cache.is_stale(agent.agent_id, max_age=max_age, now=observed_at):
            continue
        before = cache.load(agent.agent_id)
        previous_ids = {item.model_id for item in before.models} if before else set()
        if agent.runtime == "codex":
            executable = str(config.codex_multi_auth_executable or "codex-multi-auth")
            discover: Callable[[], tuple[ProviderModel, ...]] = partial(
                codex_models, executable, run=run
            )
        elif agent.runtime == "opencode":
            executable = agent.executable or "opencode"
            discover = partial(opencode_models, executable, run=run)
        else:
            executable = agent.executable or "agy"
            discover = partial(antigravity_models, executable, run=run)
        try:
            models = discover()
            snapshot = cache.store(
                agent.agent_id,
                models,
                source_version=_source_version(executable, run=run),
                observed_at=observed_at,
            )
        except (OSError, subprocess.SubprocessError, ProviderCatalogError, RuntimeError):
            cache.mark_failure(agent.agent_id, observed_at=observed_at)
            failed.append(agent.agent_id)
            continue
        refreshed.append(agent.agent_id)
        new_ids = tuple(
            item.model_id for item in snapshot.models if item.model_id not in previous_ids
        )
        if before is not None and new_ids:
            added[agent.agent_id] = new_ids

    return CatalogRefreshResult(tuple(refreshed), tuple(failed), added)
