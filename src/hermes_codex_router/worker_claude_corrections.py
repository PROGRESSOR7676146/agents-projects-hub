"""Worker-thread bridge; the original invocation retains all permissions."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .claude_correction_state import ClaudeCorrectionState
from .claude_corrections import ClaudeCorrectionCallbacks, ClaudeInputFrame
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
    store = ClaudeCorrectionState(state._connection)

    def prepare(frame: ClaudeInputFrame) -> None:
        with state._immediate_transaction():
            store.prepare(parent, token, frame, str(root))

    def claim() -> ClaudeInputFrame | None:
        # Resolve the locally selected root before the transaction that reserves
        # an input. A change cannot retarget the existing process or its hook.
        if (
            state._connection.execute(
                "SELECT 1 FROM provider_jobs child JOIN provider_jobs parent ON parent.topic_id=child.topic_id "
                "WHERE parent.job_id=? AND child.topic_sequence>parent.topic_sequence AND child.status='queued' LIMIT 1",
                (parent,),
            ).fetchone()
            is None
        ):
            return None
        revalidate_root()
        with state._immediate_transaction():
            return store.claim(parent, token)

    def receipt(identifier: str, boundary: int) -> None:
        with state._immediate_transaction():
            store.receipt(parent, token, identifier, boundary)

    def result(boundary: int) -> None:
        with state._immediate_transaction():
            store.result(parent, token, boundary)

    return ClaudeCorrectionCallbacks(prepare, claim, receipt, result)
