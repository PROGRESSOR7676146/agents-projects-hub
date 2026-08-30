from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .hub_config import HubConfig
from .state import HubState, RuntimeHealthStatus

CONTROLLER_INSTANCE_ID = "project-hub-controller"
SENDER_INSTANCE_ID = "telegram-outbox-sender"


def _project(
    classified: RuntimeHealthStatus,
    *,
    component: str,
    instance_id: str,
    runtime: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Flatten one cached row while retaining expected identity when it is missing."""
    projected: dict[str, Any] = {
        "component": component,
        "instance_id": instance_id,
        "runtime": runtime,
        "agent_id": agent_id,
        "status": classified.status,
    }
    if classified.record is not None:
        projected.update(asdict(classified.record))
        projected["status"] = classified.status
        identity_mismatch = (
            projected.get("component") != component
            or projected.get("instance_id") != instance_id
            or (runtime is not None and projected.get("runtime") != runtime)
            or (agent_id is not None and projected.get("agent_id") != agent_id)
        )
        # Configuration is authoritative. A row written under an expected key
        # with a different provider identity is degraded rather than presented
        # as a healthy instance of the wrong runtime.
        projected["component"] = component
        projected["instance_id"] = instance_id
        projected["runtime"] = runtime
        projected["agent_id"] = agent_id
        if identity_mismatch:
            projected["status"] = "degraded"
            projected["identity_mismatch"] = True
    return projected


def project_runtime_health(
    state: HubState,
    config: HubConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project configured runtime health entirely from the local SQLite cache."""
    controller = _project(
        state.runtime_health_status("controller", CONTROLLER_INSTANCE_ID, now=now),
        component="controller",
        instance_id=CONTROLLER_INSTANCE_ID,
    )
    if config.outbox_runtime == "external":
        sender = _project(
            state.runtime_health_status("sender", SENDER_INSTANCE_ID, now=now),
            component="sender",
            instance_id=SENDER_INSTANCE_ID,
            runtime="telegram",
        )
    else:
        sender = {
            "component": "sender",
            "instance_id": SENDER_INSTANCE_ID,
            "runtime": "telegram",
            "agent_id": None,
            "status": "not_configured",
        }

    workers: list[dict[str, Any]] = []
    if config.dispatch_mode == "queue" and config.queue_runtime == "external":
        for agent_id in sorted(config.external_worker_agent_ids or ("codex",)):
            agent = config.require_agent(agent_id)
            instance_id = f"{agent_id}-worker"
            workers.append(
                _project(
                    state.runtime_health_status("provider_worker", instance_id, now=now),
                    component="provider_worker",
                    instance_id=instance_id,
                    runtime=agent.runtime,
                    agent_id=agent_id,
                )
            )
    return {
        "controller": controller,
        "sender": sender,
        "provider_workers": workers,
    }
