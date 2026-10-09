"""Bounded read-only reconciliation of accepted Codex turns after stream loss."""

from __future__ import annotations

import html
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .artifacts import (
    ValidatedArtifact,
    artifact_spool_root,
    remove_spooled_artifact,
    spool_staged_artifacts,
)
from .codex_appserver import (
    CodexTurnError,
    RpcError,
    StoredTurnOutcome,
)
from .codex_failure import codex_failure_notice
from .codex_ingress_notice import append_ingress_precaution
from .diagnostic_log import survived
from .execution_journal import ExecutionJournal
from .hub_config import HubConfig
from .observed_delivery import archive_recovery_notice, retain_delivery_terminal_evidence
from .state import RECOVERED_RESULT_METADATA_JSON, HubState, StateError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _completed_visible_text(outcome: StoredTurnOutcome, artifacts_rejected: bool) -> str:
    if outcome.result is None:
        raise StateError("completed turn has no stored result")
    visible = outcome.result.text or "Codex completed the turn without visible text."
    if artifacts_rejected:
        visible += "\n\nSome staged artifacts could not be recovered; inspect the task staging."
    return visible


class ObservedTurnResults:
    """Apply exact terminal observations without owning any native RPC.

    Productive recovery, read-only observation and late control share this state
    transaction and artifact cleanup. Control sender ownership is independent.
    """

    def __init__(self, state: HubState, config: HubConfig) -> None:
        self.state = state
        self.config = config
        self.db = state._connection

    def apply_outcome(
        self,
        job_id: str,
        thread_id: str,
        turn_id: str,
        root: Path,
        outcome: StoredTurnOutcome,
        *,
        refund_deferred_attempt: bool = False,
    ) -> None:
        if outcome.status in {"active", "unknown"}:
            self._record_uncertain(job_id, outcome.status)
        else:
            artifacts: tuple[ValidatedArtifact, ...] = ()
            rejected: list[str] = []
            prior_delivery = self.db.execute(
                "SELECT status, send_started_at FROM telegram_outbox WHERE job_id=?", (job_id,)
            ).fetchone()
            # Only parked unknown is stable outside the transaction. A sending
            # lease may commit its receipt before _commit_terminal rereads it.
            preserve_delivery = prior_delivery is not None and prior_delivery["status"] == "unknown"
            if outcome.status == "completed" and not preserve_delivery:
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
                    refund_deferred_attempt=refund_deferred_attempt,
                )
            except BaseException:
                self._remove_unused_artifacts(artifacts)
                raise
            stopped_completion = self.db.execute(
                "SELECT 1 FROM provider_turn_terminal_evidence "
                "WHERE job_id=? AND terminal_status='completed'",
                (job_id,),
            ).fetchone()
            if not applied or stopped_completion is not None:
                self._remove_unused_artifacts(artifacts)

    def _remove_unused_artifacts(self, artifacts: tuple[ValidatedArtifact, ...]) -> None:
        for artifact in artifacts:
            try:
                remove_spooled_artifact(artifact.path, artifact_spool_root(self.config.state_path))
            except Exception as survived_error:
                survived("turn_observation.artifact_cleanup", survived_error)

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
        refund_deferred_attempt: bool = False,
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
                or row["session_status"] not in {"active", "satellite"}
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
            preserve_delivery = outbox["status"] == "unknown"
            if outbox["status"] == "sending":
                self.db.execute(
                    """UPDATE provider_turn_observations SET attempt_count = MAX(attempt_count - ?, 0),
                       next_check_at = ?, updated_at = ? WHERE job_id = ?""",
                    (
                        int(refund_deferred_attempt),
                        (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                        now,
                        job_id,
                    ),
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
            if preserve_delivery:
                retain_delivery_terminal_evidence(
                    self.db,
                    job_id=job_id,
                    status=outcome.status,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    root=str(root),
                    timestamp=now,
                    completed_text=outcome.result.text
                    if outcome.status == "completed" and outcome.result is not None
                    else None,
                )
                self.state._provider_job_state.complete_finished_stops(int(row["topic_id"]), now)
                self.db.execute("DELETE FROM provider_turn_observations WHERE job_id=?", (job_id,))
                return True
            stopped_completion = (
                outcome.status == "completed"
                and self.state.pending_emergency_stop_for_job(job_id) is not None
            )
            if stopped_completion:
                notice = (
                    "Stop confirmed: the exact provider turn completed before interruption. "
                    "Its result is withheld because you requested stop. "
                    "Changes already made were not undone; inspect the project before new work."
                )
            elif outcome.status == "completed":
                visible = _completed_visible_text(outcome, artifacts_rejected)
                notice = "Recovered completed Codex result:\n\n" + html.escape(visible)
            else:
                partial = ExecutionJournal(self.state).partial_text(job_id)
                error = CodexTurnError(RpcError("stream disconnected"), partial)
                notice = codex_failure_notice(error, turn_status=outcome.status, held_count=held)
            notice = append_ingress_precaution(self.state, job_id, notice)
            if len(notice) > 200_000:
                raise StateError("recovered notice exceeds delivery bound")
            archive_recovery_notice(
                self.db, job_id=job_id, outbox_id=outbox["outbox_id"], timestamp=now
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
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    new_outbox_id,
                    job_id,
                    row["agent_id"],
                    row["chat_id"],
                    row["thread_id"],
                    notice,
                    now,
                    now,
                    now,
                ),
            )
            self.state._insert_telegram_outbox_parts(
                new_outbox_id, notice, artifacts=() if stopped_completion else artifacts
            )
            if outcome.status == "completed" and not stopped_completion:
                visible = _completed_visible_text(outcome, artifacts_rejected)
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
                       VALUES (?, ?, ?, ?, 'codex', ?, ?, ?)""",
                    (
                        row["topic_id"],
                        row["agent_id"],
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
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(topic_id, observer_agent_id) DO UPDATE SET
                             last_turn_id = MAX(last_turn_id, excluded.last_turn_id),
                             updated_at = excluded.updated_at""",
                        (row["topic_id"], row["agent_id"], row["context_watermark"], now),
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
                retain_delivery_terminal_evidence(
                    self.db,
                    job_id=job_id,
                    status=outcome.status,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    root=str(root),
                    timestamp=now,
                    completed_text=outcome.result.text
                    if outcome.status == "completed" and outcome.result is not None
                    else None,
                )
            self.state._provider_job_state.complete_finished_stops(int(row["topic_id"]), now)
            self.db.execute("DELETE FROM provider_turn_observations WHERE job_id = ?", (job_id,))
            return True
