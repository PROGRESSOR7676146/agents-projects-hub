"""Worker control guard: revalidate registry, then atomically fence the exact send."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .codex_turn_controls import ActiveTurnProof, InterruptSource
from .hub_config import HubConfig
from .project_resolution import resolve_project_context
from .state import HubState
from .topic_execution import resolve_topic_execution_root


@dataclass(frozen=True, slots=True)
class ReservedIngressControl:
    owner: str
    real_stop_request: str | None


def _validated_control_root(
    state: HubState, config: HubConfig, job_id: str, proof: ActiveTurnProof, deadline: float
) -> str | None:
    topic = state.get_topic(state.get_provider_job(job_id).topic_id)
    current = resolve_project_context(
        config, state, chat_id=topic.chat_id, expected_project_id=topic.project_id
    )
    root = resolve_topic_execution_root(state, current.registry, topic)
    if str(root) != proof.root or time.monotonic() >= deadline:
        return None
    return str(root)


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
    root = _validated_control_root(state, config, job_id, proof, deadline)
    if root is None:
        return None
    return state.codex_controls.begin_interrupt(
        job_id=job_id,
        source=source,
        proof=proof,
        validated_root=root,
        invocation_token=invocation_token,
        read_claim_token=read_claim_token,
        send_deadline=deadline,
    )


def begin_codex_ingress_interrupt(
    state: HubState,
    config: HubConfig,
    *,
    job_id: str,
    proof: ActiveTurnProof,
    deadline: float,
    invocation_token: str | None = None,
    read_claim_token: str | None = None,
) -> str | None:
    root = _validated_control_root(state, config, job_id, proof, deadline)
    if root is None:
        return None
    return state.codex_ingress_control.begin_interrupt(
        job_id=job_id,
        proof=proof,
        validated_root=root,
        invocation_token=invocation_token,
        read_claim_token=read_claim_token,
        send_deadline=deadline,
    )


def begin_codex_ingress_or_stop(
    state: HubState,
    config: HubConfig,
    *,
    job_id: str,
    proof: ActiveTurnProof,
    deadline: float,
    invocation_token: str | None = None,
    read_claim_token: str | None = None,
) -> ReservedIngressControl | None:
    """A racing real stop reuses this proof and authority, never another claim."""
    pending = state.pending_emergency_stop_for_job(job_id)
    if pending is None:
        owner = begin_codex_ingress_interrupt(
            state,
            config,
            job_id=job_id,
            proof=proof,
            deadline=deadline,
            invocation_token=invocation_token,
            read_claim_token=read_claim_token,
        )
        if owner is not None:
            return ReservedIngressControl(owner, None)
        pending = state.pending_emergency_stop_for_job(job_id)
        if pending is None:
            return None
    owner = begin_codex_interrupt(
        state,
        config,
        job_id=job_id,
        source="late" if read_claim_token is not None else "live",
        proof=proof,
        deadline=deadline,
        invocation_token=invocation_token,
        read_claim_token=read_claim_token,
    )
    return None if owner is None else ReservedIngressControl(owner, pending)
