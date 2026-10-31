"""ADR 0084 impact classes for evaluated operational alerts; passive and model-free."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from .incident_runbooks import IMPACTS, incident_runbook
from .operational_alert import OperationalAlert


def _healthy_agents(runtime_health: Mapping[str, object] | None) -> dict[str, bool]:
    """Whether each configured provider agent still has at least one healthy worker."""
    workers = runtime_health.get("provider_workers") if runtime_health else None
    agents: dict[str, bool] = {}
    if isinstance(workers, list):
        for worker in workers:
            if isinstance(worker, Mapping) and isinstance(worker.get("agent_id"), str):
                agent = str(worker["agent_id"])
                agents[agent] = agents.get(agent, False) or worker.get("status") == "healthy"
    return agents


def _worker_agents(key: str, runtime_health: Mapping[str, object] | None) -> set[str]:
    """Agents of the workers an alert key names, canonicalized as the evaluator does.

    The evaluator truncates instance IDs to 128 characters, so a truncated key
    can name several workers; every candidate agent is returned.
    """
    workers = runtime_health.get("provider_workers") if runtime_health else None
    instance = key.removeprefix("runtime:provider_worker:")
    if key == instance or not isinstance(workers, list):
        return set()
    return {
        str(worker["agent_id"])
        for worker in workers
        if isinstance(worker, Mapping)
        and isinstance(worker.get("agent_id"), str)
        and str(worker.get("instance_id") or "unknown")[:128] == instance
    }


def classify_alerts(
    alerts: tuple[OperationalAlert, ...],
    runtime_health: Mapping[str, object] | None,
) -> tuple[OperationalAlert, ...]:
    """Resolve each alert's impact from its catalogue default and remaining capability.

    A failed provider worker blocks work only when its agent has no healthy
    worker left; while another slot of the same agent is healthy it is reduced
    capacity. An explicit valid impact set by the evaluator is kept.
    """
    healthy = _healthy_agents(runtime_health)
    classified: list[OperationalAlert] = []
    for alert in alerts:
        impact = alert.impact if alert.impact in IMPACTS else incident_runbook(alert.code).impact
        agents = _worker_agents(alert.key, runtime_health)
        if agents and alert.impact is None:
            # Conservative on ambiguity: any candidate agent without a healthy
            # worker makes the failure blocking.
            impact = (
                "degraded" if all(healthy.get(agent, False) for agent in agents) else "blocking"
            )
        classified.append(replace(alert, impact=impact))
    return tuple(classified)
