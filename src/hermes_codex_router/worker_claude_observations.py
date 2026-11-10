"""Adapt Claude's stream observations to the worker's existing journals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .claude_quota_observation import QuotaObservationCallback
from .claude_stream import ClaudeStreamError, ClaudeVisibleAssistant, VisibleAssistantCallback
from .execution_journal import ClaudeSessionBinding, ExecutionJournal


def claude_stream_observers(
    journal: ExecutionJournal,
    binding: ClaudeSessionBinding,
    job_id: str,
    token: str,
    project_root: Path,
    record_event: Callable[[str, str, str], None],
) -> tuple[VisibleAssistantCallback, QuotaObservationCallback]:
    def record_visible(item: ClaudeVisibleAssistant) -> None:
        if item.session_id != binding.session_id:
            raise ClaudeStreamError("claude visible message has a different session")
        try:
            journal.record_claude_item(
                job_id, token, binding.session_id, item.message_id, item.text, cwd=project_root
            )
        except Exception as exc:
            raise ClaudeStreamError("claude visible message could not be saved") from exc

    def record_quota(observation: dict[str, object]) -> None:
        record_event("info", "claude_quota_observation", json.dumps(observation, sort_keys=True))

    return record_visible, record_quota
