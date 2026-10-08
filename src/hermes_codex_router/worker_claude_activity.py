"""Optional process observer lifetime; mandatory result journal stays independent."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .claude_activity import ClaudeActivityState
from .diagnostic_log import survived
from .execution_journal import ClaudeSessionBinding
from .state_errors import StateError

if TYPE_CHECKING:
    from .state import HubState


@contextmanager
def claude_process_observation_for_turn(
    state: HubState,
    job_id: str,
    token: str,
    binding: ClaudeSessionBinding | None,
    project_root: str,
    *,
    enabled: bool,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Iterator[Callable[[], None] | None]:
    if not enabled or binding is None:
        yield None
        return
    observer = ClaudeActivityState(
        state._connection,
        transaction=state._immediate_transaction,
        state_error=StateError,
        notices=state.task_notices,
        notices_enabled=True,
    )
    observing = True

    def retire() -> None:
        try:
            observer.retire(job_id, token, now=clock())
        except Exception as error:
            survived("worker_claude_activity.retire", error)

    def on_started() -> None:
        nonlocal observing
        if not observing:
            return
        try:
            observer.open_process_observation(
                job_id, token, binding.session_id, project_root, now=clock()
            )
        except Exception as error:
            observing = False
            survived("worker_claude_activity.open", error)
            retire()

    try:
        yield on_started
    finally:
        observing = False
        retire()
