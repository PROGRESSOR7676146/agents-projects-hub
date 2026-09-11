from __future__ import annotations

import html
from pathlib import Path
from typing import Callable

from .artifacts import (
    ValidatedArtifact,
    artifact_spool_root,
    remove_spooled_artifact,
    spool_staged_artifacts,
)
from .codex_appserver import CodexAppServerClient, CodexTurnError, RpcError
from .codex_failure import CodexPreparationError, codex_failure_notice
from .execution_journal import ExecutionJournal
from .hub_config import HubConfig
from .registry import ProjectRegistry
from .state import RECOVERED_RESULT_METADATA_JSON, HubState, StateError


def checkpoint_failure_notice(state: HubState, job_id: str, error: BaseException) -> str:
    if isinstance(error, CodexPreparationError):
        return codex_failure_notice(error)
    partial = getattr(error, "partial_text", "") or ExecutionJournal(state).partial_text(job_id)
    retained = CodexTurnError(error, partial)
    if isinstance(error, CodexTurnError):
        retained.failure_reason = error.failure_reason
    return codex_failure_notice(retained)


def reconcile_codex_completion(
    state: HubState,
    config: HubConfig,
    *,
    project_root: Path,
    job_id: str,
    lease_token: str,
    agent_id: str,
    client_factory: Callable[[], CodexAppServerClient],
) -> bool:
    """Commit one exact, read-only confirmed result under the current lease."""
    journal = ExecutionJournal(state)
    checkpoint = journal.read(job_id)
    if checkpoint is None:
        return False
    canonical_root = project_root.resolve(strict=True)
    if checkpoint["project_root"] != str(canonical_root):
        raise StateError("recovery project binding changed")
    thread_id = checkpoint["provider_thread_id"]
    turn_id = checkpoint["provider_turn_id"]
    if not isinstance(thread_id, str) or not thread_id:
        raise StateError("recovery thread identity is missing")
    text = checkpoint["completed_text"]
    if text is None and isinstance(turn_id, str) and turn_id:
        client = client_factory()
        try:
            result = client.read_completed_turn(
                thread_id=thread_id,
                turn_id=turn_id,
                cwd=canonical_root,
            )
        finally:
            try:
                client.close()
            except Exception:
                pass
        if result is not None:
            text = result.text
            journal.record_completion(job_id, lease_token, text)
    if text is None:
        return False

    rejections: list[str] = []
    artifacts: tuple[ValidatedArtifact, ...] = ()
    try:
        artifacts = spool_staged_artifacts(
            canonical_root,
            job_id,
            artifact_spool_root(config.state_path),
            rejection_sink=rejections,
        )
        visible = text or "Codex completed the turn without visible text."
        if rejections:
            visible += "\n\nSome staged artifacts could not be recovered; inspect the task staging."
        job = state.get_provider_job(job_id)
        state.commit_provider_result(
            job_id,
            lease_token,
            visible_response=visible,
            sender_agent_id=agent_id,
            telegram_html="Recovered completed Codex result:\n\n" + html.escape(visible),
            provider_session_id=thread_id,
            safe_metadata_json=RECOVERED_RESULT_METADATA_JSON,
            user_excerpt=job.payload_text,
            acknowledge_context=job.context_watermark is not None,
            acknowledge_handoff=job.handoff_id is not None,
            artifacts=artifacts,
        )
    except BaseException:
        for artifact in artifacts:
            try:
                remove_spooled_artifact(artifact.path, artifact_spool_root(config.state_path))
            except Exception:
                pass
        raise
    return True


def recover_codex_job(
    state: HubState,
    config: HubConfig,
    registry: ProjectRegistry,
    agent_id: str,
    worker_id: str,
    client_factory: Callable[[], CodexAppServerClient],
) -> bool:
    """Reconcile one stale invocation. No productive provider method is called."""
    journal = ExecutionJournal(state)
    job = journal.claim_stale(agent_id, worker_id)
    if job is None:
        return False
    assert job.lease_token is not None
    token = job.lease_token
    binding_valid = False
    artifacts: tuple[ValidatedArtifact, ...] = ()
    try:
        topic = state.get_topic(job.topic_id)
        project = registry.require_project(topic.project_id)
        checkpoint = journal.read(job.job_id)
        if checkpoint is None:
            raise StateError("no durable execution identity")
        if checkpoint["project_root"] != str(project.root.resolve(strict=True)):
            raise StateError("recovery project binding changed")
        binding_valid = True
        if not reconcile_codex_completion(
            state,
            config,
            project_root=project.root,
            job_id=job.job_id,
            lease_token=token,
            agent_id=agent_id,
            client_factory=client_factory,
        ):
            raise RpcError("worker stopped without a confirmed completed turn")
    except Exception as exc:
        if state.get_provider_job(job.job_id).status != "executing":
            return True
        for artifact in artifacts:
            try:
                remove_spooled_artifact(artifact.path, artifact_spool_root(config.state_path))
            except Exception:
                state.record_runtime_event(agent_id, "warning", "recovery_spool_cleanup", "failed")
        # Missing protocol capability, unknown acceptance, active turns and root
        # mismatches cannot authorize productive replay or a successful result.
        partial = journal.partial_text(job.job_id) if binding_valid else ""
        notice = codex_failure_notice(CodexTurnError(exc, partial))
        state.terminate_provider_job_with_notice(
            job.job_id,
            token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="recovery_unconfirmed",
            sender_agent_id=agent_id,
            telegram_html=notice,
        )
    return True
