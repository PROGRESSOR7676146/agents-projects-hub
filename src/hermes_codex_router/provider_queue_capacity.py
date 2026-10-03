"""Passive capacity predicates shared by worker scheduling and queue notices."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

PROVIDER_WORKER_FAIRNESS_FRESHNESS = timedelta(minutes=2)


def parallel_worker_declarations(agent_id: str) -> tuple[str, ...]:
    if agent_id not in {"codex", "claude"}:
        return ()
    return (f"{agent_id}-worker",) + tuple(f"{agent_id}-worker-{slot}" for slot in range(2, 17))


@dataclass(frozen=True, slots=True)
class QueueCapacityConfig:
    max_parallel_roots: int
    scheduler_agents: tuple[str, ...] = ()
    agent_capacities: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderQueueCapacity:
    effective_capacity: int
    occupied_roots: int
    busy_agents: Mapping[str, int]
    busy_workers: frozenset[str]


def read_queue_capacity(
    connection: sqlite3.Connection, config: QueueCapacityConfig, *, now: datetime
) -> ProviderQueueCapacity:
    """Read scheduler inputs; never declare a worker or infer worker availability.

    Callers own the transaction. Stale execution and unresolved outcomes remain
    canonical-root blockers, separately from the scheduler's live slot count.
    """
    timestamp = now.astimezone(timezone.utc).isoformat()
    freshness = (now - PROVIDER_WORKER_FAIRNESS_FRESHNESS).astimezone(timezone.utc).isoformat()
    effective = config.max_parallel_roots
    if config.scheduler_agents:
        declaration_keys = tuple(
            dict.fromkeys(
                (
                    *config.scheduler_agents,
                    *(
                        slot
                        for agent in config.scheduler_agents
                        for slot in parallel_worker_declarations(agent)
                    ),
                )
            )
        )
        placeholders = ", ".join("?" for _ in declaration_keys)
        advertised = connection.execute(
            f"SELECT MIN(declared_capacity) FROM execution_scheduler_workers "
            f"WHERE agent_id IN ({placeholders}) AND observed_at >= ?",
            (*declaration_keys, freshness),
        ).fetchone()[0]
        if advertised is not None:
            effective = min(effective, int(advertised))
    occupied = int(
        connection.execute(
            "SELECT COUNT(DISTINCT COALESCE(topics.execution_scope, 'project:' || topics.project_id)) "
            "FROM provider_jobs jobs JOIN topics ON topics.topic_id=jobs.topic_id "
            "WHERE jobs.status IN ('leased','executing') AND jobs.lease_expires_at > ?",
            (timestamp,),
        ).fetchone()[0]
    )
    busy_agents = {
        str(row["agent_id"]): int(row["active_count"])
        for row in connection.execute(
            "SELECT agent_id, COUNT(*) AS active_count FROM provider_jobs "
            "WHERE status IN ('leased','executing') AND lease_expires_at > ? GROUP BY agent_id",
            (timestamp,),
        ).fetchall()
    }
    busy_workers = frozenset(
        str(row["lease_owner"])
        for row in connection.execute(
            "SELECT DISTINCT lease_owner FROM provider_jobs "
            "WHERE status IN ('leased','executing') AND lease_expires_at > ?",
            (timestamp,),
        ).fetchall()
    )
    return ProviderQueueCapacity(effective, occupied, busy_agents, busy_workers)
