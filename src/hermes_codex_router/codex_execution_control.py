"""One accepted turn's mandatory result callbacks and independent stop observer."""

from __future__ import annotations

from collections.abc import Callable

from .codex_appserver import CodexAppServerClient, TurnResult
from .codex_live_control import CodexLiveControl, ControlClient
from .execution_journal import ExecutionJournal
from .hub_config import HubConfig
from .state import HubState, ProviderJobRecord
from .worker_execution import ProviderTurnStopped, wait_for_codex_provider_turn


def wait_for_controlled_codex_turn(
    client: CodexAppServerClient,
    state: HubState,
    config: HubConfig,
    *,
    journal: ExecutionJournal,
    job: ProviderJobRecord,
    worker_id: str,
    thread_id: str,
    turn_id: str,
    transport_mode: str | None,
    client_factory: Callable[[], ControlClient],
) -> TurnResult:
    token = job.lease_token
    assert token is not None
    control = CodexLiveControl(
        config=config,
        state_factory=lambda: HubState.open_existing(
            config.state_path,
            codex_permission_profile=config.codex_permission_profile,
            contention_timeout_seconds=0.1,
        ),
        client_factory=client_factory,
        job=job,
        worker_id=worker_id,
        thread_id=thread_id,
        turn_id=turn_id,
        transport_mode=transport_mode,
        close_owned_turn_client=client.close,
    )
    with control.running():
        client.on_visible_item = lambda item_id, text, phase: journal.record_item(
            job.job_id, token, item_id, text, phase
        )
        client.on_completed = lambda result: journal.record_completion(
            job.job_id, token, result.text
        )
        try:
            result = wait_for_codex_provider_turn(client, turn_id)
            journal.record_completion(job.job_id, token, result.text)
        finally:
            client.on_visible_item = None
            client.on_completed = None
    # This branch is reached only after exact completion, never on mere ACK.
    request = control.confirmed_interrupt_request or state.pending_emergency_stop_for_job(
        job.job_id
    )
    if request is not None:
        raise ProviderTurnStopped(request)
    control.raise_deferred_failure()
    return result
