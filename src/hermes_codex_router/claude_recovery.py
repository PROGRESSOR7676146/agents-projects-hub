from __future__ import annotations

import html

from .artifacts import (
    ValidatedArtifact,
    artifact_spool_root,
    remove_spooled_artifact,
    spool_staged_artifacts,
)
from .codex_failure import uncertain_provider_notice
from .diagnostic_log import survived
from .execution_journal import ExecutionJournal
from .hub_config import HubConfig
from .project_resolution import resolve_project_context
from .registry import ProjectRegistry
from .state import RECOVERED_RESULT_METADATA_JSON, HubState, ProviderJobRecord, StateError
from .topic_execution import resolve_topic_execution_root


def recover_claude_job(
    state: HubState,
    config: HubConfig,
    registry: ProjectRegistry,
    agent_id: str,
    worker_id: str,
) -> bool:
    """Recover only committed native evidence; never invoke or resume a provider."""
    journal = ExecutionJournal(state)
    job = journal.claim_stale(agent_id, worker_id)
    if job is None:
        return False
    return _recover_leased_claude_job(state, config, registry, agent_id, job)


def recover_claude_completion(
    state: HubState,
    config: HubConfig,
    registry: ProjectRegistry,
    agent_id: str,
    job_id: str,
    token: str,
) -> bool:
    """Use a saved terminal result after a caught fault, under the existing lease.

    False means there is no saved completion to recover. A handled completion
    stays executing if local delivery preparation fails again; lease expiry is
    the bounded retry boundary, never a second provider invocation.
    """
    job = state.get_provider_job(job_id)
    checkpoint = ExecutionJournal(state).read(job_id)
    if (
        job.status != "executing"
        or job.lease_token != token
        or job.agent_id != agent_id
        or checkpoint is None
        or checkpoint["completed_text"] is None
    ):
        return False
    return _recover_leased_claude_job(state, config, registry, agent_id, job)


def _recover_leased_claude_job(
    state: HubState,
    config: HubConfig,
    registry: ProjectRegistry,
    agent_id: str,
    job: ProviderJobRecord,
) -> bool:
    journal = ExecutionJournal(state)
    assert job.lease_token is not None
    token = job.lease_token
    partial = ""
    artifacts: tuple[ValidatedArtifact, ...] = ()
    committed = False
    terminal_proven = False
    try:
        topic = state.get_topic(job.topic_id)
        resolved = resolve_project_context(
            config, state, chat_id=topic.chat_id, expected_project_id=topic.project_id
        )
        registry = resolved.registry
        root = resolve_topic_execution_root(state, registry, topic)
        checkpoint = journal.read(job.job_id)
        if checkpoint is None or checkpoint["provider_thread_id"] is None:
            raise StateError("Claude recovery identity is missing")
        native = checkpoint["provider_thread_id"]
        # Validate the exact current session, generation, writer, lease, and
        # canonical root before exposing either provisional or completed text.
        partial = journal.validated_claude_partial(job.job_id, token, native, cwd=root)
        text = checkpoint["completed_text"]
        if text is None:
            raise StateError("Claude recovery has no confirmed terminal result")
        terminal_proven = True
        rejections: list[str] = []
        artifacts = spool_staged_artifacts(
            root, job.job_id, artifact_spool_root(config.state_path), rejection_sink=rejections
        )
        visible = text or "Claude completed the invocation without visible text."
        material_notice = checkpoint["claude_material_notice"]
        if material_notice is not None:
            visible += material_notice
        elif state.incoming_materials_for_job(job.job_id):
            visible += "\n\nAttachment availability was not saved; recovery cannot confirm which attachments were supplied."
        if rejections:
            visible += "\n\nSome staged artifacts could not be recovered; inspect the task staging."
        result = state.commit_provider_result(
            job.job_id,
            token,
            visible_response=visible,
            sender_agent_id=agent_id,
            telegram_html="Recovered completed Claude result:\n\n" + html.escape(visible),
            provider_session_id=native,
            safe_metadata_json=RECOVERED_RESULT_METADATA_JSON,
            user_excerpt=job.payload_text,
            acknowledge_context=job.context_watermark is not None,
            acknowledge_handoff=job.handoff_id is not None,
            artifacts=artifacts,
        )
        # A covering stop can cancel instead of publishing. Its terminality is
        # proven by this invocation's saved completion, never its session UUID.
        committed = result is not None
    except Exception:
        if state.get_provider_job(job.job_id).status != "executing":
            return True
        if terminal_proven:
            # A local preparation/commit error cannot erase validated native
            # completion or turn it into execution uncertainty. Preserve the
            # lease/root; another bounded stale-recovery cycle retries delivery.
            return True
        notice = uncertain_provider_notice(config.require_agent(agent_id).display_name)
        if partial:
            notice += "\n\nSaved incomplete visible response:\n" + html.escape(partial)
        state.terminate_provider_job_with_notice(
            job.job_id,
            token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="claude_recovery_unconfirmed",
            sender_agent_id=agent_id,
            telegram_html=notice,
        )
    finally:
        if not committed:
            for artifact in artifacts:
                try:
                    remove_spooled_artifact(artifact.path, artifact_spool_root(config.state_path))
                except Exception as error:
                    survived("claude_recovery.artifact_cleanup", error)
    return True
