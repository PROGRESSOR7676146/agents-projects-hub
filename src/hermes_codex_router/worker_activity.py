"""Scoped worker callback lifetime for passive, accepted Codex activity."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .codex_activity import CodexActivityEvent
from .codex_appserver import CodexAppServerClient
from .diagnostic_log import survived
from .hub_config import HubConfig
from .preacceptance_approvals import PreacceptanceApprovalState, RuntimeEpoch
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
    runtime_epoch: RuntimeEpoch | None = None,
    prepared_thread_id: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Iterator[Callable[[str, str], None]]:
    """Install before start, bind after the journal accepts, clear on every exit.

    Normal activity is buffered until journal acceptance. The separate early
    callback stores only fenced observations while submission is pending; it
    cannot create an accepted checkpoint or answer a native approval.
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
        state._connection,
        transaction=state._immediate_transaction,
        state_error=StateError,
        selected_codex_profile=lambda: state.codex_permission_profile,
    )
    activity = TaskActivityState(
        state._connection,
        transaction=state._immediate_transaction,
        state_error=StateError,
        notices=notices,
        notices_enabled=True,
    )

    early = (
        PreacceptanceApprovalState(state, notices_enabled=True)
        if runtime_epoch is not None
        else None
    )
    scope_id = None
    observing = True
    promoted = False
    retired = False
    activity_retired = False

    def retire_activity() -> None:
        nonlocal activity_retired
        if not observing and not activity_retired:
            try:
                activity.retire_observation(job_id, token, now=clock())
                activity_retired = True
            except Exception as error:
                survived("worker_activity.activity_retire", error)

    def retire_scope() -> None:
        nonlocal retired
        if (
            not promoted
            and not retired
            and scope_id is not None
            and early is not None
            and runtime_epoch is not None
        ):
            try:
                early.retire(scope_id, runtime_epoch, now=clock())
                retired = True
            except Exception as error:
                survived("worker_activity.scope_retire", error)

    def disable() -> None:
        nonlocal observing
        observing = False
        client.on_activity = None
        client.on_preacceptance_approval = None
        retire_activity()
        retire_scope()

    if early is not None and runtime_epoch is not None:
        try:
            if prepared_thread_id is None:
                raise StateError("early approvals require explicit prepared-thread context")
            scope_id = early.open_scope(
                job_id,
                token,
                runtime_epoch,
                provider_thread_id=prepared_thread_id,
                project_root=str(root),
                now=clock(),
            )
        except Exception as error:
            survived("worker_activity.scope_open", error)
            disable()

    def accepted(thread_id: str, turn_id: str) -> None:
        nonlocal promoted
        if not observing:
            return
        try:
            now = clock()
            with state._immediate_transaction():
                activity.bind_accepted_in_transaction(
                    job_id, token, thread_id, turn_id, str(root), now=now
                )
                did_promote = (
                    early is not None
                    and runtime_epoch is not None
                    and early.promote_in_transaction(
                        scope_id,
                        runtime_epoch,
                        activity,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        now=now,
                    )
                )
            promoted = did_promote
        except Exception as error:
            survived("worker_activity.accepted_binding", error)
            disable()

    def observe(event: CodexActivityEvent) -> None:
        if not observing:
            return
        try:
            activity.record_activity(job_id, token, event, now=clock())
        except Exception as error:
            survived("worker_activity.observe", error)
            disable()

    def observe_early(event: CodexActivityEvent) -> None:
        if observing and early is not None and runtime_epoch is not None:
            try:
                early.observe(scope_id, runtime_epoch, event, now=clock())
            except Exception as error:
                survived("worker_activity.observe_early", error)
                disable()

    client.on_activity = observe if observing else None
    client.on_preacceptance_approval = observe_early if observing and early is not None else None
    try:
        yield accepted
    finally:
        client.on_activity = None
        client.on_preacceptance_approval = None
        retire_activity()
        retire_scope()
