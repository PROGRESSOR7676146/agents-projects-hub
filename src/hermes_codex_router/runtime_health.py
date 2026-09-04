from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from .hub_config import HubConfig
from .state import HubState, RuntimeHealthStatus

CONTROLLER_INSTANCE_ID = "project-hub-controller"
SENDER_INSTANCE_ID = "telegram-outbox-sender"
MONITOR_INSTANCE_ID = "operations-monitor"


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
    monitor = _project(
        state.runtime_health_status(
            "monitor",
            MONITOR_INSTANCE_ID,
            now=now,
            degraded_after=timedelta(minutes=7),
            stale_after=timedelta(minutes=15),
        ),
        component="monitor",
        instance_id=MONITOR_INSTANCE_ID,
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
    required = [controller, monitor]
    if sender["status"] != "not_configured":
        required.append(sender)
    required.extend(workers)
    release_identities: set[tuple[str, str, str]] = set()
    unknown_release = False
    for item in required:
        version = item.get("release_version")
        git_sha = item.get("release_git_sha")
        built_at = item.get("release_built_at")
        if (
            not item.get("release_clean")
            or not isinstance(version, str)
            or not version
            or not isinstance(git_sha, str)
            or not git_sha
            or not isinstance(built_at, str)
            or not built_at
        ):
            unknown_release = True
            continue
        release_identities.add((version, git_sha, built_at))
    if unknown_release:
        deployment_status = "unknown"
    elif len(release_identities) == 1:
        deployment_status = "converged"
    else:
        deployment_status = "mixed"
    common_release = next(iter(release_identities)) if deployment_status == "converged" else None
    return {
        "controller": controller,
        "monitor": monitor,
        "sender": sender,
        "provider_workers": workers,
        "deployment_revision": {
            "status": deployment_status,
            "required_components": len(required),
            "package_version": None if common_release is None else common_release[0],
            "git_sha": None if common_release is None else common_release[1],
            "built_at": None if common_release is None else common_release[2],
            "clean_tree": deployment_status == "converged",
        },
    }
