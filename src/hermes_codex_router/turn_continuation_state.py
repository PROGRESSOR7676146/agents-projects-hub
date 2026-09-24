"""Durable owner choice after a read-only confirmed terminal Codex turn error."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import HubState, ProviderJobRecord


CONTINUATION_PROMPT = (
    "The previous Codex turn stopped with an error after possible partial effects. "
    "This is a new owner-requested turn in the same session, not a replay of the old task. "
    "First inspect the current project state and changes already made. Explain what remains, "
    "then continue only where safe; ask the owner before any action whose prior completion "
    "cannot be determined."
)


class TurnContinuationState:
    """One HubState-owned SQLite transaction for a notice-bound continuation."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    def session_status(self, session_id: str) -> tuple[int, int]:
        """Count confirmed failed old turns and paused queued work for owner UI."""
        failed = int(
            self.connection.execute(
                """SELECT COUNT(*) FROM provider_turn_terminal_evidence evidence
                   JOIN provider_jobs jobs ON jobs.job_id = evidence.job_id
                   WHERE jobs.session_id = ? AND jobs.status = 'indeterminate'""",
                (session_id,),
            ).fetchone()[0]
        )
        held = int(
            self.connection.execute(
                """SELECT COUNT(*) FROM provider_job_holds holds
                   JOIN provider_jobs jobs ON jobs.job_id = holds.job_id
                   JOIN agent_sessions session ON session.topic_id = jobs.topic_id
                   WHERE session.session_id = ? AND jobs.status IN ('queued', 'retry_wait')""",
                (session_id,),
            ).fetchone()[0]
        )
        return failed, held

    def source_for_notice(
        self, *, chat_id: int, thread_id: int, notice_message_id: int
    ) -> str | None:
        row = self.connection.execute(
            """SELECT jobs.job_id FROM telegram_outbox_parts parts
               JOIN telegram_outbox outbox ON outbox.outbox_id = parts.outbox_id
               JOIN provider_jobs jobs ON jobs.job_id = outbox.job_id
               JOIN topics ON topics.topic_id = jobs.topic_id
               JOIN provider_turn_terminal_evidence evidence ON evidence.job_id = jobs.job_id
               WHERE outbox.status = 'delivered' AND parts.telegram_message_id = ?
                 AND outbox.chat_id = ? AND topics.thread_id = ?
                 AND outbox.sender_agent_id = 'codex' AND jobs.status = 'indeterminate'
               LIMIT 1""",
            (notice_message_id, chat_id, thread_id),
        ).fetchone()
        return str(row["job_id"]) if row is not None else None

    def continue_from_notice(
        self,
        *,
        source_job_id: str,
        chat_id: int,
        thread_id: int,
        notice_message_id: int,
        reply_message_id: int,
        canonical_root: Path,
    ) -> tuple[ProviderJobRecord, bool, int]:
        from .state import StateError

        root = canonical_root.resolve(strict=True)
        now = datetime.now(timezone.utc).isoformat()
        with self.state._immediate_transaction():
            if (
                self.source_for_notice(
                    chat_id=chat_id, thread_id=thread_id, notice_message_id=notice_message_id
                )
                != source_job_id
            ):
                raise StateError("continuation notice identity changed")
            old = self.connection.execute(
                """SELECT jobs.*, topics.thread_id, topics.execution_scope,
                          topics.project_id,
                          sessions.status AS session_status,
                          sessions.writer_mode, sessions.provider_session_id AS current_thread,
                          sessions.generation AS current_generation,
                          evidence.provider_thread_id AS evidence_thread,
                          evidence.project_root AS evidence_root
                   FROM provider_jobs jobs
                   JOIN topics ON topics.topic_id = jobs.topic_id
                   JOIN agent_sessions sessions ON sessions.session_id = jobs.session_id
                   JOIN provider_turn_terminal_evidence evidence ON evidence.job_id = jobs.job_id
                   WHERE jobs.job_id = ?""",
                (source_job_id,),
            ).fetchone()
            if (
                old is None
                or old["status"] != "indeterminate"
                or old["agent_id"] != "codex"
                or int(old["chat_id"]) != chat_id
                or int(old["thread_id"]) != thread_id
                or old["session_status"] != "active"
                or old["writer_mode"] != "telegram"
                or int(old["current_generation"]) != int(old["session_generation"])
                or old["current_thread"] != old["evidence_thread"]
                or old["evidence_root"] != str(root)
                or old["execution_scope"] != "root:" + str(root)
            ):
                raise StateError("continuation session or root binding changed")
            from .session_adoption_state import CodexSessionOrigins

            origin = CodexSessionOrigins(self.state).get(str(old["session_id"]))
            if origin is not None and (
                origin.provider_thread_id != old["current_thread"]
                or origin.canonical_root != root
                or origin.project_id != old["project_id"]
            ):
                raise StateError("continuation saved Codex origin changed")
            prior = self.connection.execute(
                "SELECT continuation_job_id FROM provider_job_continuations "
                "WHERE source_job_id = ?",
                (source_job_id,),
            ).fetchone()
            if prior is not None:
                return self.state.get_provider_job(str(prior["continuation_job_id"])), False, 0
            conflict = self.connection.execute(
                """SELECT 1 FROM provider_jobs jobs
                   JOIN topics topics ON topics.topic_id = jobs.topic_id
                   WHERE topics.execution_scope = ? AND jobs.job_id != ? AND (
                     jobs.status IN ('leased', 'executing', 'result_ready')
                     OR (jobs.status IN ('queued', 'retry_wait') AND NOT EXISTS (
                       SELECT 1 FROM provider_job_holds holds WHERE holds.job_id = jobs.job_id
                     ))
                     OR (jobs.status = 'indeterminate' AND NOT EXISTS (
                       SELECT 1 FROM provider_job_resolutions resolution
                       WHERE resolution.job_id = jobs.job_id
                     ) AND NOT EXISTS (
                       SELECT 1 FROM provider_turn_terminal_evidence terminal
                       WHERE terminal.job_id = jobs.job_id
                     ))
                   ) LIMIT 1""",
                ("root:" + str(root), source_job_id),
            ).fetchone()
            writer = self.connection.execute(
                """SELECT 1 FROM agent_sessions sessions
                   JOIN topics ON topics.topic_id = sessions.topic_id
                   WHERE topics.execution_scope = ? AND sessions.session_id != ?
                     AND sessions.status IN ('active', 'satellite')
                     AND sessions.writer_mode != 'telegram' LIMIT 1""",
                ("root:" + str(root), old["session_id"]),
            ).fetchone()
            dispatch = self.connection.execute(
                """SELECT 1 FROM turn_dispatches dispatches
                   JOIN topics ON topics.topic_id = dispatches.topic_id
                   WHERE topics.execution_scope = ? AND dispatches.status = 'running' LIMIT 1""",
                ("root:" + str(root),),
            ).fetchone()
            if conflict or writer or dispatch:
                raise StateError("execution root has another writer or unreviewed work")
            held_count = int(
                self.connection.execute(
                    """SELECT COUNT(*) FROM provider_job_holds holds
                       JOIN provider_jobs jobs ON jobs.job_id = holds.job_id
                       JOIN topics ON topics.topic_id = jobs.topic_id
                       WHERE topics.execution_scope = ? AND jobs.status IN ('queued', 'retry_wait')""",
                    ("root:" + str(root),),
                ).fetchone()[0]
            )
            observed = self.connection.execute(
                "SELECT 1 FROM observed_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, reply_message_id),
            ).fetchone()
            if observed is not None:
                raise StateError("continuation reply was already consumed")
            counter = self.connection.execute(
                "SELECT next_sequence FROM topic_queue_counters WHERE topic_id = ?",
                (old["topic_id"],),
            ).fetchone()
            if counter is None:
                raise StateError("continuation topic has no queue counter")
            job_id = str(uuid.uuid4())
            self.connection.execute(
                """INSERT INTO provider_jobs (
                     job_id, idempotency_key, chat_id, message_id, topic_id, topic_sequence,
                     agent_id, session_id, session_generation, provider_session_id,
                     model, effort, payload_text, status, attempt_count, max_attempts,
                     created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?, ?, ?, 'queued', 0, 1, ?, ?)""",
                (
                    job_id,
                    "continuation:" + source_job_id,
                    chat_id,
                    reply_message_id,
                    old["topic_id"],
                    counter["next_sequence"],
                    old["session_id"],
                    old["session_generation"],
                    old["current_thread"],
                    old["model"],
                    old["effort"],
                    CONTINUATION_PROMPT,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE topic_queue_counters SET next_sequence = next_sequence + 1, "
                "updated_at = ? WHERE topic_id = ?",
                (now, old["topic_id"]),
            )
            self.connection.execute(
                """INSERT INTO provider_job_inputs
                   (job_id, chat_id, message_id, part_index, input_text, received_at)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (job_id, chat_id, reply_message_id, CONTINUATION_PROMPT, now),
            )
            self.connection.execute(
                """INSERT INTO provider_job_continuations
                   (source_job_id, continuation_job_id, chat_id, message_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (source_job_id, job_id, chat_id, reply_message_id, now),
            )
            self.connection.execute(
                """INSERT INTO observed_messages
                   (chat_id, message_id, observer_agent_id, observed_at)
                   VALUES (?, ?, 'hub', ?)""",
                (chat_id, reply_message_id, now),
            )
            return self.state.get_provider_job(job_id), True, held_count
