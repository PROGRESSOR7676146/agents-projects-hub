"""Maintain an existing invocation lease; never reclaim or replay provider work."""

from __future__ import annotations

import threading
from collections.abc import Callable

from .diagnostic_log import survived
from .hub_config import HubConfig
from .sqlite_contention import is_sqlite_contention
from .state import HubState, ProviderJobRecord


def maintain_provider_lease(
    config: HubConfig,
    job_id: str,
    token: str,
    stop: threading.Event,
    *,
    on_renewed: Callable[[HubState, ProviderJobRecord], None] | None = None,
    on_failure: Callable[[Exception], None] | None = None,
) -> None:
    state: HubState | None = None
    try:
        while not stop.is_set():
            try:
                if state is None:
                    state = HubState.open_existing(
                        config.state_path,
                        codex_permission_profile=config.codex_permission_profile,
                        contention_timeout_seconds=1,
                    )
                refreshed = state.heartbeat_provider_job(job_id, token, lease_seconds=120)
                if on_renewed is not None:
                    on_renewed(state, refreshed)
            except Exception as error:
                if not is_sqlite_contention(error):
                    if on_failure is not None:
                        on_failure(error)
                    else:
                        survived("provider_lease_heartbeat.failed", error)
                    return
                # Retry only the same lease renewal. The state guard still
                # refuses expired or replaced ownership and completed work.
                survived("provider_lease_heartbeat.contention", error)
                stop.wait(1)
                continue
            stop.wait(30)
    finally:
        if state is not None:
            state.close()
