"""Explicit, narrow phases shared by durable provider workers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .hub_config import HubConfig
from .models import Project, ProjectRegistry
from .project_resolution import resolve_project_context
from .state import HubState, ProviderJobRecord, TopicRecord
from .topic_execution import resolve_topic_execution_root


@dataclass(frozen=True, slots=True)
class WorkerExecutionTarget:
    """Persisted identity plus the currently authorized project registration."""

    registry: ProjectRegistry
    project: Project
    topic: TopicRecord


def require_provider_job_lease(
    job: ProviderJobRecord,
    *,
    error_factory: Callable[[str], Exception],
) -> str:
    """Return the immutable lease capability or fail before any worker effect."""
    if job.lease_token is None:
        raise error_factory("leased provider job has no lease token")
    return job.lease_token


def resolve_external_worker_target(
    config: HubConfig,
    state: HubState,
    job: ProviderJobRecord,
) -> WorkerExecutionTarget:
    """Refresh an external worker's project binding while its job is only leased."""
    topic = state.get_topic(job.topic_id)
    resolved = resolve_project_context(
        config,
        state,
        chat_id=topic.chat_id,
        expected_project_id=topic.project_id,
    )
    return WorkerExecutionTarget(resolved.registry, resolved.project, topic)


def resolve_embedded_worker_target(
    state: HubState,
    registry: ProjectRegistry,
    job: ProviderJobRecord,
) -> WorkerExecutionTarget:
    """Capture the embedded worker's registered target before execution."""
    topic = state.get_topic(job.topic_id)
    project = registry.require_project(topic.project_id)
    return WorkerExecutionTarget(registry, project, topic)


def revalidate_worker_execution_root(
    state: HubState,
    target: WorkerExecutionTarget,
) -> WorkerExecutionTarget:
    """Revalidate the base root or durable lane immediately before preparation."""
    execution_root = resolve_topic_execution_root(state, target.registry, target.topic)
    return replace(target, project=replace(target.project, root=execution_root))


__all__ = [
    "WorkerExecutionTarget",
    "require_provider_job_lease",
    "resolve_embedded_worker_target",
    "resolve_external_worker_target",
    "revalidate_worker_execution_root",
]
