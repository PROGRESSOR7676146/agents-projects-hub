"""Scoped worker callback lifetime for passive, accepted Codex activity."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .codex_activity import CodexActivityEvent
from .codex_appserver import CodexAppServerClient
from .hub_config import HubConfig
from .state import HubState, StateError
from .task_activity import TaskActivityState
from .task_lifecycle import TaskLifecycleState


@contextmanager
def codex_activity_for_turn(
    client: CodexAppServerClient,
    state: HubState,
    config: HubConfig,
    job_id: str,
    token: str,
    root: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Iterator[Callable[[str, str], None]]:
    """Install before start, bind after the journal accepts, clear on every exit.

    The client buffers observations until wait_for_turn, so the worker records
    the exact accepted checkpoint before callbacks run. An approval that blocks
    turn/start before acceptance remains outside this accepted-turn observer;
    the native approval host retains ownership of that request.
    """
    enabled = (
        config.hub_bot is not None
        and config.queue_runtime == "external"
        and config.outbox_runtime == "external"
    )
    if not enabled:
        yield lambda thread_id, turn_id: None
        return
    notices = TaskLifecycleState(
        state._connection, transaction=state._immediate_transaction, state_error=StateError
    )
    activity = TaskActivityState(
        state._connection,
        transaction=state._immediate_transaction,
        state_error=StateError,
        notices=notices,
        notices_enabled=True,
    )

    def accepted(thread_id: str, turn_id: str) -> None:
        activity.bind_accepted(job_id, token, thread_id, turn_id, str(root), now=clock())

    def observe(event: CodexActivityEvent) -> None:
        activity.record_activity(job_id, token, event, now=clock())

    client.on_activity = observe
    try:
        yield accepted
    finally:
        client.on_activity = None
