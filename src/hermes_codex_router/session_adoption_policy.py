from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hub_config import HubConfig


def supports_adoption(config: HubConfig) -> bool:
    try:
        agent = config.require_agent("codex")
    except KeyError:
        return False
    return (
        config.dispatch_mode == "queue"
        and config.queue_runtime == "external"
        and "codex" in (config.external_worker_agent_ids or ("codex",))
        and config.outbox_runtime == "external"
        and agent.runtime == "codex"
        and not agent.managed_externally
    )


def validate_adoption_mode(config: HubConfig, connection: sqlite3.Connection | None = None) -> None:
    """Reject unsupported execution before constructing any provider or transport.

    Retained archived origins still require the policy-aware runtime. This is
    deliberately conservative during rollback and never deletes evidence.
    """
    if supports_adoption(config):
        return
    if connection is None:
        if not config.state_path.is_file():
            return
        with closing(
            sqlite3.connect(config.state_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as opened:
            validate_adoption_mode(config, opened)
        return
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_session_origins'"
    ).fetchone()
    if (
        exists is not None
        and connection.execute("SELECT 1 FROM codex_session_origins LIMIT 1").fetchone() is not None
    ):
        raise ValueError("adopted Codex sessions require external queue workers and outbox")
