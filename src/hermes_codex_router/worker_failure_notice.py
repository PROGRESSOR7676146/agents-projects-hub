"""Shared generic worker failure preparation; HubState owns the outcome commit."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .codex_recovery import checkpoint_failure_notice
from .codex_retry_policy import preparation_retry_binding

if TYPE_CHECKING:
    from .hub_config import HubConfig
    from .state import HubState, ProviderJobRecord
    from .worker_execution import WorkerFailureClassification


def commit_worker_failure_notice(
    state: HubState,
    config: HubConfig,
    job: ProviderJobRecord,
    token: str,
    *,
    root: Path,
    error: Exception,
    failure: WorkerFailureClassification,
    turn_status: str,
    fallback_notice: str,
    error_detail: str | None = None,
) -> ProviderJobRecord:
    if failure.notice == "incoming_material":
        notice = (
            "Incoming material integrity validation failed; "
            "the provider was not started. Send the material again."
        )
    elif failure.notice == "checkpoint":
        notice = checkpoint_failure_notice(state, job.job_id, error, turn_status=turn_status)
    else:
        notice = fallback_notice
    return state.terminate_provider_job_with_notice(
        job.job_id,
        token,
        status=failure.status,
        error_class=failure.error_class,
        error_code=failure.error_code,
        error_detail=error_detail,
        terminal_turn_status=turn_status if turn_status in {"failed", "interrupted"} else None,
        preparation_retry=preparation_retry_binding(
            error, root=root, model_provider=config.codex_model_provider
        )
        if job.agent_id == "codex"
        else None,
        sender_agent_id=job.agent_id,
        telegram_html=notice,
    )
