"""Worker control guard: revalidate registry, then atomically fence the exact send."""

from __future__ import annotations

import time

from .codex_turn_controls import ActiveTurnProof, InterruptSource
from .hub_config import HubConfig
from .project_resolution import resolve_project_context
from .state import HubState
from .topic_execution import resolve_topic_execution_root


def begin_codex_interrupt(
    state: HubState,
    config: HubConfig,
    *,
    job_id: str,
    source: InterruptSource,
    proof: ActiveTurnProof,
    deadline: float,
    invocation_token: str | None = None,
    read_claim_token: str | None = None,
) -> str | None:
    topic = state.get_topic(state.get_provider_job(job_id).topic_id)
    current = resolve_project_context(
        config, state, chat_id=topic.chat_id, expected_project_id=topic.project_id
    )
    root = resolve_topic_execution_root(state, current.registry, topic)
    if str(root) != proof.root or time.monotonic() >= deadline:
        return None
    return state.codex_controls.begin_interrupt(
        job_id=job_id,
        source=source,
        proof=proof,
        validated_root=str(root),
        invocation_token=invocation_token,
        read_claim_token=read_claim_token,
        send_deadline=deadline,
    )
