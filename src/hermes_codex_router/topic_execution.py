"""Execution-time resolution of a topic's only authorized working root."""

from __future__ import annotations

from pathlib import Path

from .registry import ExecutionRootError, ProjectRegistry, validate_execution_root
from .state import HubState, TopicRecord
from .worktrees import validate_worktree_execution_root


def resolve_topic_execution_root(
    state: HubState, registry: ProjectRegistry, topic: TopicRecord
) -> Path:
    """Return the validated base root or the topic's explicit bound lane root.

    A topic scope is durable ownership evidence, not filesystem authorization.
    The registry and (for a lane) the persisted binding still have to prove the
    path immediately before provider, staging, recovery, or local-resume use.
    """
    project = registry.require_project(topic.project_id)
    base_root = validate_execution_root(registry, project)
    lane = state.active_lane_for_topic(topic.topic_id)
    if lane is None:
        if topic.execution_scope not in {
            f"root:{base_root}",
            f"project:{project.project_id}",
        }:
            raise ExecutionRootError()
        return base_root
    if str(lane["project_id"]) != project.project_id or int(lane["topic_id"]) != topic.topic_id:
        raise ExecutionRootError()
    lane_root = validate_worktree_execution_root(
        registry,
        project,
        str(lane["lane_id"]),
        Path(str(lane["worktree_path"])),
    )
    if topic.execution_scope != f"root:{lane_root}":
        raise ExecutionRootError()
    return lane_root
