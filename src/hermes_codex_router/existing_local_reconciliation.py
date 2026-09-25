"""Exact, provider-free adoption of an already opened native Codex session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .codex_appserver import (
    CodexAppServerClient,
    CodexThreadMetadata,
    StoredTurnOutcome,
    UnixWebSocketTransport,
)
from .codex_session_adoption import open_adoption_state
from .hub_config import HubConfig
from .local_transfer import local_resume_command
from .project_resolution import resolve_project_context
from .session_adoption_state import CodexSessionOrigins
from .state import HubState, StateError
from .topic_execution import resolve_topic_execution_root


@dataclass(frozen=True, slots=True)
class ExistingLocalResult:
    session_id: str
    provider_thread_id: str
    generation: int
    writer_mode: str
    old_job_status: str
    old_turn_status: str
    changed: bool
    resume_command: str | None = None


def inspect_owning_server(
    config: HubConfig, thread_id: str, turn_id: str, root: Path
) -> tuple[CodexThreadMetadata, StoredTurnOutcome]:
    """The owning socket only; no stdio fallback or productive method."""
    client = CodexAppServerClient(
        UnixWebSocketTransport(config.codex_socket_path, timeout=10),
        approval_policy="never",
        model_provider=config.codex_model_provider,
    )
    try:
        client.initialize()
        metadata = client.read_thread_metadata(thread_id=thread_id, cwd=root)
        outcome = client.read_turn_outcome(thread_id=thread_id, turn_id=turn_id, cwd=root)
        return metadata, outcome
    finally:
        client.close()


def reconcile_existing_local(
    config: HubConfig,
    *,
    session_id: str,
    provider_thread_id: str,
    old_job_id: str,
    expected_generation: int,
    expected_root: Path,
    apply: bool = False,
    confirm_cli_closed: bool = False,
    confirm_remote_idle: bool = False,
    inspector=inspect_owning_server,
) -> ExistingLocalResult:
    """Preview or atomically claim local ownership of the exact active Hub session.

    A standalone CLI requires the owner's assertion that it has been closed at
    an idle boundary. An existing remote CLI requires the owner's assertion it
    is idle and will send no turn during the short transfer. Neither assertion
    is inferred from PID absence or provider metadata alone.
    """
    if apply and confirm_cli_closed == confirm_remote_idle:
        raise StateError("choose exactly one proven CLI ownership boundary")
    root = expected_root.resolve(strict=True)
    with open_adoption_state(config.state_path) as state:
        session = state.get_session(session_id)
        topic = state.get_topic(session.topic_id)
        resolved = resolve_project_context(
            config,
            state,
            chat_id=topic.chat_id,
            expected_project_id=topic.project_id,
            expected_root=root,
        )
        execution_root = resolve_topic_execution_root(state, resolved.registry, topic)
        old = state.get_provider_job(old_job_id)
        checkpoint = state._connection.execute(
            "SELECT provider_thread_id, provider_turn_id, project_root "
            "FROM provider_execution_checkpoints WHERE job_id = ?",
            (old_job_id,),
        ).fetchone()
        if (
            session.status != "active"
            or session.agent_id != "codex"
            or session.generation != expected_generation
            or session.provider_session_id != provider_thread_id
            or session.writer_mode not in {"telegram", "local"}
            or old.session_id != session_id
            or old.session_generation != expected_generation
            or old.status != "indeterminate"
            or checkpoint is None
            or checkpoint["provider_thread_id"] != provider_thread_id
            or not checkpoint["provider_turn_id"]
            or checkpoint["project_root"] != str(root)
            or execution_root != root
            or topic.execution_scope != "root:" + str(root)
        ):
            raise StateError("exact active session, old turn, or canonical root mismatch")
        origin = CodexSessionOrigins(state).get(session_id)
        if origin is not None and (
            origin.provider_thread_id != provider_thread_id
            or origin.canonical_root != root
            or origin.project_id != topic.project_id
        ):
            raise StateError("saved Codex origin mismatch")
        turn_id = str(checkpoint["provider_turn_id"])
        expected_error = (old.error_class, old.error_code, old.error_detail)
        saved_notice = state.get_telegram_outbox_for_job(old_job_id)
        saved_job_count = len(state.provider_jobs_for_topic(topic.topic_id))
        expected_model_provider = (
            origin.model_provider if origin is not None else config.codex_model_provider
        )
    metadata, outcome = inspector(config, provider_thread_id, turn_id, root)
    if (
        metadata.thread_id != provider_thread_id
        or metadata.cwd != root
        or (
            expected_model_provider is not None
            and metadata.model_provider != expected_model_provider
        )
        or metadata.status not in {"idle", "notLoaded"}
        or outcome.status not in {"failed", "interrupted"}
    ):
        raise StateError("owning server has no proven idle terminal turn")
    resume = local_resume_command(
        "codex",
        None,
        provider_thread_id,
        root,
        model_provider=config.codex_model_provider,
        model=session.model,
        codex_socket_path=config.codex_socket_path,
    )
    if not apply:
        return ExistingLocalResult(
            session_id,
            provider_thread_id,
            expected_generation,
            session.writer_mode,
            old.status,
            outcome.status,
            False,
        )
    with open_adoption_state(config.state_path, writable=True) as state:
        changed = _claim_exact_local(
            state,
            session_id=session_id,
            provider_thread_id=provider_thread_id,
            old_job_id=old_job_id,
            turn_id=turn_id,
            generation=expected_generation,
            root=root,
            terminal_status=outcome.status,
            expected_model=session.model,
            expected_effort=session.effort,
        )
        current = state.get_session(session_id)
        old = state.get_provider_job(old_job_id)
        read_checkpoint = state._connection.execute(
            "SELECT provider_thread_id, provider_turn_id, project_root "
            "FROM provider_execution_checkpoints WHERE job_id = ?",
            (old_job_id,),
        ).fetchone()
        evidence = state._connection.execute(
            """SELECT terminal_status, provider_thread_id, provider_turn_id, project_root
               FROM provider_turn_terminal_evidence WHERE job_id = ?""",
            (old_job_id,),
        ).fetchone()
        notice = state.get_telegram_outbox_for_job(old_job_id)
        if (
            current.writer_mode != "local"
            or current.provider_session_id != provider_thread_id
            or current.generation != expected_generation
            or old.status != "indeterminate"
            or (old.error_class, old.error_code, old.error_detail) != expected_error
            or read_checkpoint is None
            or tuple(read_checkpoint) != (provider_thread_id, turn_id, str(root))
            or evidence is None
            or tuple(evidence) != (outcome.status, provider_thread_id, turn_id, str(root))
            or notice.outbox_id != saved_notice.outbox_id
            or notice.telegram_html != saved_notice.telegram_html
            or len(state.provider_jobs_for_topic(topic.topic_id)) != saved_job_count
        ):
            raise StateError("local reconciliation read-back failed")
        return ExistingLocalResult(
            session_id,
            provider_thread_id,
            expected_generation,
            current.writer_mode,
            old.status,
            outcome.status,
            changed,
            resume.display,
        )


def _claim_exact_local(
    state: HubState,
    *,
    session_id: str,
    provider_thread_id: str,
    old_job_id: str,
    turn_id: str,
    generation: int,
    root: Path,
    terminal_status: str,
    expected_model: str,
    expected_effort: str,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with state._immediate_transaction():
        row = state._connection.execute(
            """SELECT sessions.session_id, sessions.status, sessions.agent_id,
                      sessions.generation, sessions.writer_mode,
                      sessions.provider_session_id, sessions.model, sessions.effort,
                      topics.execution_scope,
                      topics.project_id
               FROM agent_sessions sessions
               JOIN topics ON topics.topic_id = sessions.topic_id
               WHERE sessions.session_id = ?""",
            (session_id,),
        ).fetchone()
        checkpoint = state._connection.execute(
            """SELECT jobs.status, jobs.session_id, jobs.session_generation,
                      evidence.provider_thread_id, evidence.provider_turn_id,
                      evidence.project_root
               FROM provider_jobs jobs
               JOIN provider_execution_checkpoints evidence ON evidence.job_id = jobs.job_id
               WHERE jobs.job_id = ?""",
            (old_job_id,),
        ).fetchone()
        if (
            row is None
            or checkpoint is None
            or row["status"] != "active"
            or row["agent_id"] != "codex"
            or int(row["generation"]) != generation
            or row["provider_session_id"] != provider_thread_id
            or row["model"] != expected_model
            or row["effort"] != expected_effort
            or row["execution_scope"] != "root:" + str(root)
            or row["writer_mode"] not in {"telegram", "local"}
            or checkpoint["status"] != "indeterminate"
            or checkpoint["session_id"] != session_id
            or int(checkpoint["session_generation"]) != generation
            or checkpoint["provider_thread_id"] != provider_thread_id
            or checkpoint["provider_turn_id"] != turn_id
            or checkpoint["project_root"] != str(root)
        ):
            raise StateError("local reconciliation snapshot changed")
        origin = CodexSessionOrigins(state).get(session_id)
        if origin is not None and (
            origin.provider_thread_id != provider_thread_id
            or origin.canonical_root != root
            or origin.project_id != row["project_id"]
        ):
            raise StateError("saved Codex origin changed")
        state._connection.execute(
            """INSERT OR IGNORE INTO provider_job_holds (job_id, cause_job_id, held_at)
               SELECT jobs.job_id, ?, ? FROM provider_jobs jobs
               JOIN topics ON topics.topic_id = jobs.topic_id
               WHERE topics.execution_scope = ? AND jobs.job_id != ?
                 AND jobs.status IN ('queued', 'retry_wait')""",
            (old_job_id, now, "root:" + str(root), old_job_id),
        )
        conflict = state._connection.execute(
            """SELECT 1 FROM provider_jobs jobs
               JOIN topics ON topics.topic_id = jobs.topic_id
               WHERE topics.execution_scope = ? AND jobs.job_id != ? AND (
                 (jobs.status IN ('queued', 'retry_wait') AND NOT EXISTS (
                   SELECT 1 FROM provider_job_holds holds WHERE holds.job_id = jobs.job_id
                     AND holds.decision = 'pending'
                 ))
                 OR jobs.status IN ('leased', 'executing', 'result_ready')
                 OR (jobs.status = 'indeterminate' AND NOT EXISTS (
                   SELECT 1 FROM provider_job_resolutions resolution
                   WHERE resolution.job_id = jobs.job_id
                 ) AND NOT EXISTS (
                   SELECT 1 FROM provider_turn_terminal_evidence terminal
                   WHERE terminal.job_id = jobs.job_id
                 ))
               ) LIMIT 1""",
            ("root:" + str(root), old_job_id),
        ).fetchone()
        writer = state._connection.execute(
            """SELECT 1 FROM agent_sessions sessions
               JOIN topics ON topics.topic_id = sessions.topic_id
               WHERE topics.execution_scope = ? AND sessions.session_id != ?
                 AND sessions.status IN ('active', 'satellite')
                 AND sessions.writer_mode != 'telegram' LIMIT 1""",
            ("root:" + str(root), session_id),
        ).fetchone()
        dispatch = state._connection.execute(
            """SELECT 1 FROM turn_dispatches dispatches
               JOIN topics ON topics.topic_id = dispatches.topic_id
               WHERE topics.execution_scope = ? AND dispatches.status = 'running' LIMIT 1""",
            ("root:" + str(root),),
        ).fetchone()
        if conflict or writer or dispatch:
            raise StateError("execution root has another writer or pending work")
        prior = state._connection.execute(
            "SELECT terminal_status FROM provider_turn_terminal_evidence WHERE job_id = ?",
            (old_job_id,),
        ).fetchone()
        if prior is not None and prior["terminal_status"] != terminal_status:
            raise StateError("terminal turn evidence changed")
        if prior is None:
            state._connection.execute(
                """INSERT INTO provider_turn_terminal_evidence
                   (job_id, terminal_status, provider_thread_id, provider_turn_id,
                    project_root, observed_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (old_job_id, terminal_status, provider_thread_id, turn_id, str(root), now),
            )
        if row["writer_mode"] == "local":
            return False
        changed = state._connection.execute(
            """UPDATE agent_sessions SET writer_mode = 'local', updated_at = ?
               WHERE session_id = ? AND writer_mode = 'telegram'""",
            (now, session_id),
        )
        if changed.rowcount != 1:
            raise StateError("local writer claim lost")
        return True
