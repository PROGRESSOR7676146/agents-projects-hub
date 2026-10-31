"""Worker-thread bridge; the original invocation retains all permissions."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .claude_correction_state import ClaudeCorrectionState
from .claude_corrections import ClaudeAdmissionClosed, ClaudeCorrectionCallbacks, ClaudeInputFrame
from .hub_config import HubConfig
from .state import HubState
from .state_provider_jobs import ProviderJobRecord
from .worker_execution import resolve_external_worker_target, revalidate_worker_execution_root


def corrections_for_turn(
    config: HubConfig,
    state: HubState,
    job: ProviderJobRecord,
    token: str,
    root: Path,
    *,
    runtime: str,
) -> ClaudeCorrectionCallbacks | None:
    if runtime != "claude" or not config.claude_stream_input:
        return None

    def revalidate() -> None:
        target = resolve_external_worker_target(config, state, job)
        revalidate_worker_execution_root(state, target)
        if Path(target.project.root) != root:
            raise ValueError("Claude correction root changed")

    return correction_callbacks(state, job.job_id, token, root, revalidate_root=revalidate)


def correction_callbacks(
    state: HubState,
    parent: str,
    token: str,
    root: Path,
    *,
    revalidate_root: Callable[[], None],
) -> ClaudeCorrectionCallbacks:
    store = ClaudeCorrectionState(state._connection, visibility=state.queue_visibility)

    def prepare(frame: ClaudeInputFrame) -> None:
        with state._immediate_transaction():
            store.prepare(parent, token, frame, str(root))

    def claim() -> ClaudeInputFrame | None:
        # Resolve the locally selected root before the transaction that reserves
        # an input. A change cannot retarget the existing process or its hook.
        try:
            waiting = state._connection.execute(
                "SELECT 1 FROM provider_jobs child JOIN provider_jobs parent ON parent.topic_id=child.topic_id "
                "WHERE parent.job_id=? AND child.topic_sequence>parent.topic_sequence AND child.status='queued' LIMIT 1",
                (parent,),
            ).fetchone()
            if waiting is not None:
                revalidate_root()
        except Exception as exc:
            # No reservation transaction has started, hence no correction byte
            # can exist. Optional admission must not abort the original job.
            raise ClaudeAdmissionClosed() from exc
        if waiting is None:
            return None
        try:
            with state._immediate_transaction():
                frame = store.claim(parent, token)
        except Exception as exc:
            # A failed COMMIT may be ambiguous. Only a successful read proving
            # no persisted reservation permits nonfatal FIFO fallback.
            if store.child(parent) is not None:
                raise
            raise ClaudeAdmissionClosed() from exc
        if frame is None:
            # The FIFO head is incompatible. Do not take a write lock/revalidate
            # it repeatedly; all remaining messages keep ordinary FIFO.
            raise ClaudeAdmissionClosed()
        return frame

    def receipt(identifier: str, boundary: int) -> None:
        with state._immediate_transaction():
            store.receipt(parent, token, identifier, boundary)

    def result(boundary: int) -> None:
        with state._immediate_transaction():
            store.result(parent, token, boundary)

    return ClaudeCorrectionCallbacks(prepare, claim, receipt, result)
