"""Follow owner renames of bound forum topics; titles are display metadata only."""

from __future__ import annotations

from .hub_config import HubConfig
from .project_resolution import ProjectResolutionError, resolve_project_context
from .state import HubState
from .telegram import TopicRename


def observe_topic_rename(config: HubConfig, state: HubState, rename: TopicRename) -> bool:
    """Store an authorized rename of a topic in a bound project group.

    Topic identity stays the numeric (chat_id, thread_id); routing, sessions and
    execution scope never depend on the title, so a rename changes display only.
    """
    if not config.is_authorized(rename.sender_id, rename.chat_id, rename.thread_id):
        return False
    try:
        resolved = resolve_project_context(config, state, chat_id=rename.chat_id)
    except ProjectResolutionError:
        return False
    existing = state.find_topic(rename.chat_id, rename.thread_id)
    if existing is not None and existing.project_id != resolved.project.project_id:
        return False
    # An existing topic keeps its execution scope: only the title changes.
    state.observe_topic(
        project_id=resolved.project.project_id,
        chat_id=rename.chat_id,
        thread_id=rename.thread_id,
        title=rename.title,
        execution_root=resolved.project.root if existing is None else None,
    )
    return True
