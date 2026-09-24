"""Bounded read-only reconciliation of accepted Codex turns after stream loss."""

from __future__ import annotations

import html
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .artifacts import (
    ValidatedArtifact,
    artifact_spool_root,
    remove_spooled_artifact,
    spool_staged_artifacts,
)
from .codex_appserver import (
    CodexAppServerClient,
    CodexTurnError,
    RpcError,
    StoredTurnOutcome,
    UnixWebSocketTransport,
)
from .codex_failure import codex_failure_notice
from .execution_journal import ExecutionJournal
from .hub_config import HubConfig
from .project_resolution import resolve_project_context
from .state import RECOVERED_RESULT_METADATA_JSON, HubState, StateError
from .topic_execution import resolve_topic_execution_root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def owning_read_client(config: HubConfig) -> CodexAppServerClient:
    """Attach only to the owning socket; Controller never launches a provider process."""
    client = CodexAppServerClient(
        UnixWebSocketTransport(config.codex_socket_path, timeout=10),
        approval_policy="never",
        model_provider=config.codex_model_provider,
    )
    client.initialize()
    return client


class TurnObservation:
    """Keep all state changes on the HubState connection; provider reads stay outside SQL."""

    def __init__(self, state: HubState, config: HubConfig) -> None:
        self.state = state
        self.config = config
        self.db = state._connection

    def topic_unconfirmed(self, topic_id: int) -> bool:
        return (
            self.db.execute(
                """SELECT 1 FROM provider_jobs jobs
               WHERE jobs.topic_id = ? AND jobs.status = 'indeterminate'
                 AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolution
                                 WHERE resolution.job_id = jobs.job_id)
                 AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal
                                 WHERE terminal.job_id = jobs.job_id)
               LIMIT 1""",
                (topic_id,),
            ).fetchone()
            is not None
        )

    def _claim(self) -> tuple[str, str, str, Path] | None:
        now = _now()
        with self.state._immediate_transaction():
            row = self.db.execute(
                """SELECT observations.job_id, checkpoint.provider_thread_id,
                          checkpoint.provider_turn_id, checkpoint.project_root
                   FROM provider_turn_observations observations
                   JOIN provider_jobs jobs ON jobs.job_id = observations.job_id
                   JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id = jobs.job_id
                   JOIN agent_sessions sessions ON sessions.session_id = jobs.session_id
                   WHERE observations.attempt_count < 3 AND observations.next_check_at <= ?
                     AND jobs.status = 'indeterminate' AND jobs.agent_id = 'codex'
                     AND sessions.status = 'active' AND sessions.writer_mode = 'telegram'
                     AND sessions.generation = jobs.session_generation
                     AND sessions.provider_session_id = checkpoint.provider_thread_id
                     AND checkpoint.provider_turn_id IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal
                                     WHERE terminal.job_id = jobs.job_id)
                     AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolution
                                     WHERE resolution.job_id = jobs.job_id)
                   ORDER BY observations.next_check_at, observations.job_id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                """UPDATE provider_turn_observations
                   SET attempt_count = attempt_count + 1,
                       next_check_at = ?, updated_at = ? WHERE job_id = ?""",
                (
                    (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                    now,
                    row["job_id"],
                ),
            )
            return (
                str(row["job_id"]),
                str(row["provider_thread_id"]),
                str(row["provider_turn_id"]),
                Path(str(row["project_root"])),
            )

    def run_once(self, client_factory: Callable[[], CodexAppServerClient]) -> bool:
        claimed = self._claim()
        if claimed is None:
            return False
        self._observe(claimed, client_factory)
        return True

    def observe_topic(
        self, topic_id: int, client_factory: Callable[[], CodexAppServerClient]
    ) -> bool:
        """User-initiated /local check; it never resumes or starts a turn."""
        rows = self.db.execute(
            """SELECT jobs.job_id, checkpoint.provider_thread_id,
                      checkpoint.provider_turn_id, checkpoint.project_root
               FROM provider_jobs jobs
               JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id = jobs.job_id
               WHERE jobs.topic_id = ? AND jobs.status = 'indeterminate'
                 AND checkpoint.provider_turn_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolution
                                 WHERE resolution.job_id = jobs.job_id)
                 AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal
                                 WHERE terminal.job_id = jobs.job_id)""",
            (topic_id,),
        ).fetchall()
        if len(rows) > 1:
            raise StateError("multiple unconfirmed turns require separate review")
        if not rows:
            return False
        row = rows[0]
        self._observe(
            (
                str(row["job_id"]),
                str(row["provider_thread_id"]),
                str(row["provider_turn_id"]),
                Path(str(row["project_root"])),
            ),
            client_factory,
        )
        return True

    def _observe(
        self,
        claimed: tuple[str, str, str, Path],
        client_factory: Callable[[], CodexAppServerClient],
    ) -> None:
        job_id, thread_id, turn_id, root = claimed
        try:
            job = self.state.get_provider_job(job_id)
            topic = self.state.get_topic(job.topic_id)
            resolved = resolve_project_context(
                self.config,
                self.state,
                chat_id=topic.chat_id,
                expected_project_id=topic.project_id,
                expected_root=root,
            )
            if resolve_topic_execution_root(self.state, resolved.registry, topic) != root:
                raise StateError("observation project root changed")
            client = client_factory()
            try:
                outcome = client.read_turn_outcome(thread_id=thread_id, turn_id=turn_id, cwd=root)
            finally:
                client.close()
        except Exception:
            self._record_uncertain(job_id, "unknown")
            return
        if outcome.status in {"active", "unknown"}:
            self._record_uncertain(job_id, outcome.status)
        else:
            artifacts: tuple[ValidatedArtifact, ...] = ()
            rejected: list[str] = []
            if outcome.status == "completed":
                try:
                    artifacts = spool_staged_artifacts(
                        root,
                        job_id,
                        artifact_spool_root(self.config.state_path),
                        rejection_sink=rejected,
                    )
                except Exception:
                    rejected.append("unavailable")
            try:
                applied = self._commit_terminal(
                    job_id,
                    thread_id,
                    turn_id,
                    root,
                    outcome,
                    artifacts=artifacts,
                    artifacts_rejected=bool(rejected),
                )
            except BaseException:
                self._remove_unused_artifacts(artifacts)
                raise
            if not applied:
                self._remove_unused_artifacts(artifacts)

    def _remove_unused_artifacts(self, artifacts: tuple[ValidatedArtifact, ...]) -> None:
        for artifact in artifacts:
            try:
                remove_spooled_artifact(artifact.path, artifact_spool_root(self.config.state_path))
            except Exception:
                pass

    def _record_uncertain(self, job_id: str, status: str) -> None:
        with self.state._immediate_transaction():
            self.db.execute(
                """UPDATE provider_turn_observations SET last_status = ?, updated_at = ?
                   WHERE job_id = ?""",
                (status, _now(), job_id),
            )

    def _commit_terminal(
        self,
        job_id: str,
        thread_id: str,
        turn_id: str,
        root: Path,
        outcome: StoredTurnOutcome,
        *,
        artifacts: tuple[ValidatedArtifact, ...] = (),
        artifacts_rejected: bool = False,
    ) -> bool:
        now = _now()
        with self.state._immediate_transaction():
            row = self.db.execute(
                """SELECT jobs.*, topics.thread_id, topics.execution_scope,
                          sessions.status AS session_status, sessions.writer_mode,
                          sessions.generation AS current_generation,
                          sessions.provider_session_id AS current_thread,
                          checkpoint.provider_thread_id AS checkpoint_thread,
                          checkpoint.provider_turn_id AS checkpoint_turn,
                          checkpoint.project_root AS checkpoint_root
                   FROM provider_jobs jobs
                   JOIN topics ON topics.topic_id = jobs.topic_id
                   JOIN agent_sessions sessions ON sessions.session_id = jobs.session_id
                   JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id = jobs.job_id
                   WHERE jobs.job_id = ?""",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "indeterminate"
                or row["session_status"] != "active"
                or row["writer_mode"] != "telegram"
                or int(row["session_generation"]) != int(row["current_generation"])
                or row["current_thread"] != thread_id
                or row["checkpoint_thread"] != thread_id
                or row["checkpoint_turn"] != turn_id
                or row["checkpoint_root"] != str(root)
                or row["execution_scope"] != "root:" + str(root)
            ):
                raise StateError("observed turn binding changed")
            resolved = self.db.execute(
                "SELECT 1 FROM provider_job_resolutions WHERE job_id = ?", (job_id,)
            ).fetchone()
            competing = self.db.execute(
                """SELECT 1 FROM provider_jobs jobs
                   JOIN topics ON topics.topic_id = jobs.topic_id
                   WHERE topics.execution_scope = ? AND jobs.job_id != ?
                     AND jobs.status IN ('leased', 'executing') LIMIT 1""",
                (row["execution_scope"], job_id),
            ).fetchone()
            if resolved is not None or competing is not None:
                raise StateError("observed turn has a later resolution or active writer")
            existing = self.db.execute(
                "SELECT 1 FROM provider_turn_terminal_evidence WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                self.db.execute(
                    "DELETE FROM provider_turn_observations WHERE job_id = ?", (job_id,)
                )
                return False
            outbox = self.db.execute(
                "SELECT * FROM telegram_outbox WHERE job_id = ?", (job_id,)
            ).fetchone()
            if outbox is None:
                raise StateError("uncertain job lost its notice")
            if outbox["status"] == "sending":
                self.db.execute(
                    """UPDATE provider_turn_observations SET attempt_count = attempt_count - 1,
                       next_check_at = ?, updated_at = ? WHERE job_id = ?""",
                    ((datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), now, job_id),
                )
                return False
            self.db.execute(
                """INSERT OR IGNORE INTO provider_job_holds (job_id, cause_job_id, held_at)
                   SELECT tail.job_id, ?, ? FROM provider_jobs tail
                   JOIN topics ON topics.topic_id = tail.topic_id
                   WHERE topics.execution_scope = ? AND tail.job_id != ?
                     AND tail.status IN ('queued', 'retry_wait')""",
                (job_id, now, row["execution_scope"], job_id),
            )
            held = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM provider_job_holds WHERE cause_job_id = ?", (job_id,)
                ).fetchone()[0]
            )
            if outcome.status == "completed":
                if outcome.result is None:
                    raise StateError("completed turn has no stored result")
                visible = outcome.result.text or "Codex completed the turn without visible text."
                if artifacts_rejected:
                    visible += "\n\nSome staged artifacts could not be recovered; inspect the task staging."
                notice = "Recovered completed Codex result:\n\n" + html.escape(visible)
            else:
                partial = ExecutionJournal(self.state).partial_text(job_id)
                error = CodexTurnError(RpcError("stream disconnected"), partial)
                notice = codex_failure_notice(error, turn_status=outcome.status, held_count=held)
            if len(notice) > 200_000:
                raise StateError("recovered notice exceeds delivery bound")
            self.db.execute(
                """INSERT INTO provider_recovery_notices
                   (job_id, outbox_id, telegram_html, delivery_status,
                    telegram_message_id, saved_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    outbox["outbox_id"],
                    outbox["telegram_html"],
                    outbox["status"],
                    outbox["telegram_message_id"],
                    now,
                ),
            )
            self.db.execute(
                "DELETE FROM telegram_outbox_parts WHERE outbox_id = ?", (outbox["outbox_id"],)
            )
            self.db.execute(
                "DELETE FROM telegram_outbox WHERE outbox_id = ?", (outbox["outbox_id"],)
            )
            new_outbox_id = str(uuid.uuid4())
            self.db.execute(
                """INSERT INTO telegram_outbox
                   (outbox_id, job_id, sender_agent_id, chat_id, thread_id,
                    telegram_html, status, available_at, created_at, updated_at)
                   VALUES (?, ?, 'codex', ?, ?, ?, 'pending', ?, ?, ?)""",
                (new_outbox_id, job_id, row["chat_id"], row["thread_id"], notice, now, now, now),
            )
            self.state._insert_telegram_outbox_parts(new_outbox_id, notice, artifacts=artifacts)
            if outcome.status == "completed":
                assert outcome.result is not None
                visible = outcome.result.text or "Codex completed the turn without visible text."
                if artifacts_rejected:
                    visible += "\n\nSome staged artifacts could not be recovered; inspect the task staging."
                self.db.execute(
                    """INSERT INTO provider_job_results
                       (result_id, job_id, visible_response, provider_session_id,
                        safe_metadata_json, context_watermark, handoff_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        job_id,
                        visible,
                        thread_id,
                        RECOVERED_RESULT_METADATA_JSON,
                        row["context_watermark"],
                        row["handoff_id"],
                        now,
                    ),
                )
                self.db.execute(
                    """INSERT INTO external_turn_excerpts
                       (topic_id, agent_id, provider_session_id, model, provider,
                        user_excerpt, response_excerpt, created_at)
                       VALUES (?, 'codex', ?, ?, 'codex', ?, ?, ?)""",
                    (
                        row["topic_id"],
                        thread_id,
                        row["model"],
                        str(row["payload_text"])[-2000:],
                        visible,
                        now,
                    ),
                )
                self.state._incoming_material_state.mark_job_consumed(job_id, timestamp=now)
                if row["context_watermark"] is not None:
                    self.db.execute(
                        """INSERT INTO visible_context_cursors
                           (topic_id, observer_agent_id, last_turn_id, updated_at)
                           VALUES (?, 'codex', ?, ?)
                           ON CONFLICT(topic_id, observer_agent_id) DO UPDATE SET
                             last_turn_id = MAX(last_turn_id, excluded.last_turn_id),
                             updated_at = excluded.updated_at""",
                        (row["topic_id"], row["context_watermark"], now),
                    )
                if row["handoff_id"] is not None:
                    self.db.execute(
                        "DELETE FROM pending_handoffs WHERE handoff_id = ?",
                        (row["handoff_id"],),
                    )
                self.db.execute(
                    "UPDATE provider_jobs SET status = 'result_ready', updated_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
            else:
                self.db.execute(
                    """INSERT INTO provider_turn_terminal_evidence
                       (job_id, terminal_status, provider_thread_id, provider_turn_id,
                        project_root, observed_at) VALUES (?, ?, ?, ?, ?, ?)""",
                    (job_id, outcome.status, thread_id, turn_id, str(root), now),
                )
            self.db.execute("DELETE FROM provider_turn_observations WHERE job_id = ?", (job_id,))
            return True
