"""Saved text-only preparation retries with exact notice and execution bindings."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .codex_retry_policy import PreparationRetryBinding
from .provider_admission_state import ProviderAdmissionState
from .state_errors import StateError

if TYPE_CHECKING:
    from .state import HubState, ProviderJobRecord


_UNSAVED_REPLACEMENT_CONTEXT = (
    "Saved-task retry is unavailable: preparation changed the Codex thread, "
    "and Hub has no saved context snapshot for that change. Send a new request "
    "containing the complete task and relevant context."
)


class PreparationRetryRefused(StateError):
    """A saved failed request cannot safely become a new invocation."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.public_message = message


class PreexecutionRetryState:
    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    def source_for_notice(
        self, *, chat_id: int, thread_id: int, notice_message_id: int
    ) -> str | None:
        row = self.connection.execute(
            """SELECT jobs.job_id FROM telegram_outbox_parts parts
               JOIN telegram_outbox outbox ON outbox.outbox_id=parts.outbox_id
               JOIN provider_jobs jobs ON jobs.job_id=outbox.job_id
               JOIN topics ON topics.topic_id=jobs.topic_id
               WHERE outbox.status='delivered' AND parts.delivered_at IS NOT NULL
                 AND parts.telegram_message_id=? AND outbox.chat_id=?
                 AND outbox.thread_id=? AND topics.thread_id=?
                 AND outbox.sender_agent_id='codex' AND jobs.agent_id='codex'
                 AND jobs.status='failed' AND jobs.error_class='pre_execution'
               LIMIT 1""",
            (notice_message_id, chat_id, thread_id, thread_id),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def has_execution_evidence(self, job_id: str) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 WHERE EXISTS (
                 SELECT 1 FROM provider_execution_checkpoints WHERE job_id=?
                   AND (provider_turn_id IS NOT NULL OR completed_text IS NOT NULL)
               ) OR EXISTS (SELECT 1 FROM provider_visible_items WHERE job_id=?)
                 OR EXISTS (SELECT 1 FROM provider_job_results WHERE job_id=?)
                 OR EXISTS (SELECT 1 FROM task_activity WHERE job_id=?)
                 OR EXISTS (SELECT 1 FROM provider_turn_terminal_evidence WHERE job_id=?)
                 OR EXISTS (SELECT 1 FROM preacceptance_requests requests
                   JOIN preacceptance_scopes scopes ON scopes.scope_id=requests.scope_id
                   WHERE scopes.job_id=?)""",
                (job_id,) * 6,
            ).fetchone()
            is not None
        )

    def has_materials(self, job_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM incoming_materials WHERE job_id=? LIMIT 1", (job_id,)
            ).fetchone()
            is not None
        )

    def record_ticket_in_transaction(
        self, job: sqlite3.Row, binding: PreparationRetryBinding, timestamp: str
    ) -> str:
        if not self.connection.in_transaction:
            raise StateError("preparation retry ticket requires its failure transaction")
        job_id = str(job["job_id"])
        if self.has_materials(job_id):
            return (
                "\n\nRetry: This request included materials. Send the original task and "
                "materials again; Hub will not run an incomplete saved request."
            )
        session = self.state.get_session(str(job["session_id"]))
        topic = self.state.get_topic(int(job["topic_id"]))
        root = str(binding.canonical_root.resolve(strict=True))
        checkpoint = self.connection.execute(
            "SELECT * FROM provider_execution_checkpoints WHERE job_id=?", (job_id,)
        ).fetchone()
        if (
            job["agent_id"] != "codex"
            or topic.execution_scope not in {"root:" + root, "project:" + topic.project_id}
            or session.topic_id != topic.topic_id
            or session.agent_id != "codex"
            or session.generation != job["session_generation"]
            or session.status not in {"active", "satellite"}
            or session.writer_mode != "telegram"
            or session.model != job["model"]
            or session.effort != job["effort"]
            or not (
                session.codex_permission_profile
                == job["codex_permission_profile"]
                == self.state.codex_permission_profile
            )
            or (checkpoint is None and session.provider_session_id != job["provider_session_id"])
            or (
                checkpoint is not None
                and (
                    checkpoint["project_root"] != root
                    or checkpoint["provider_thread_id"] != session.provider_session_id
                    or checkpoint["codex_permission_profile"] != job["codex_permission_profile"]
                )
            )
        ):
            return "\n\nRetry is unavailable: the saved execution binding changed. Inspect /status."
        context_refusal = self._context_refusal(job_id, session.provider_session_id)
        if context_refusal is not None:
            return "\n\n" + context_refusal
        inputs = self._inputs(job_id)
        if inputs == "[]":
            return (
                "\n\nRetry is unavailable: the saved source input is missing. Send the task again."
            )
        self.connection.execute(
            """INSERT INTO provider_preexecution_retry_tickets
               (source_job_id,project_id,canonical_root,session_id,session_generation,
                expected_thread_id,model,effort,codex_permission_profile,model_provider,
                payload_text,context_watermark,handoff_id,source_inputs_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                topic.project_id,
                root,
                session.session_id,
                session.generation,
                session.provider_session_id,
                job["model"],
                job["effort"],
                job["codex_permission_profile"],
                binding.model_provider,
                job["payload_text"],
                job["context_watermark"],
                job["handoff_id"],
                inputs,
                timestamp,
            ),
        )
        return (
            "\n\nRetry: Reply exactly retry to this notice to submit the saved task text "
            "in the same session. Tool permissions will still need fresh human decisions."
        )

    def _inputs(self, job_id: str) -> str:
        rows = self.connection.execute(
            "SELECT chat_id,message_id,part_index,input_text,received_at "
            "FROM provider_job_inputs WHERE job_id=? ORDER BY part_index",
            (job_id,),
        ).fetchall()
        return json.dumps([list(row) for row in rows], ensure_ascii=True, separators=(",", ":"))

    def _context_refusal(self, source_id: str, prepared_thread_id: str | None) -> str | None:
        """Check inherited context without revalidating ancestors against today's session."""
        seen: set[str] = set()
        current = source_id
        for _ in range(64):
            if current in seen:
                break
            row = self.connection.execute(
                """SELECT jobs.provider_session_id,tickets.source_job_id AS ticket_source,
                          tickets.expected_thread_id,retries.source_job_id AS retry_parent
                   FROM provider_jobs jobs
                   LEFT JOIN provider_preexecution_retry_tickets tickets
                     ON tickets.source_job_id=jobs.job_id
                   LEFT JOIN provider_preexecution_retries retries
                     ON retries.child_job_id=jobs.job_id
                   WHERE jobs.job_id=?""",
                (current,),
            ).fetchone()
            if row is None or (seen and row["ticket_source"] is None):
                break
            expected = prepared_thread_id if not seen else row["expected_thread_id"]
            if row["provider_session_id"] is not None and row["provider_session_id"] != expected:
                return _UNSAVED_REPLACEMENT_CONTEXT
            seen.add(current)
            if row["retry_parent"] is None:
                return None
            current = str(row["retry_parent"])
        return (
            "Saved-task retry is unavailable: retry ancestry cannot be verified within "
            "its safety bound. Send a new request containing the complete task and relevant context."
        )

    def _validate(
        self, source_id: str, *, canonical_root: Path, model_provider: str | None
    ) -> sqlite3.Row:
        ticket = self.connection.execute(
            "SELECT * FROM provider_preexecution_retry_tickets WHERE source_job_id=?",
            (source_id,),
        ).fetchone()
        if self.has_materials(source_id):
            raise PreparationRetryRefused(
                "Retry unavailable: this request included materials. Send the original "
                "task and materials again; no incomplete task was started."
            )
        if ticket is None:
            raise PreparationRetryRefused(
                "Retry unavailable: this old failure has no verified saved retry binding. "
                "Send the original task again; no new task was started."
            )
        old = self.state.get_provider_job(source_id)
        session = self.state.get_session(ticket["session_id"])
        topic = self.state.get_topic(old.topic_id)
        root = str(canonical_root.resolve(strict=True))
        if (
            old.status != "failed"
            or old.error_class != "pre_execution"
            or old.error_code != "CodexPreparationError"
            or old.agent_id != "codex"
            or self.has_execution_evidence(source_id)
            or root != ticket["canonical_root"]
            or topic.project_id != ticket["project_id"]
            or topic.execution_scope not in {"root:" + root, "project:" + topic.project_id}
            or model_provider != ticket["model_provider"]
            or session.topic_id != old.topic_id
            or session.agent_id != "codex"
            or session.session_id != old.session_id
            or session.generation != old.session_generation
            or session.generation != ticket["session_generation"]
            or session.status not in {"active", "satellite"}
            or session.writer_mode != "telegram"
            or session.provider_session_id != ticket["expected_thread_id"]
            or session.model != ticket["model"]
            or session.effort != ticket["effort"]
            or not (
                old.codex_permission_profile
                == ticket["codex_permission_profile"]
                == session.codex_permission_profile
                == self.state.codex_permission_profile
            )
            or old.payload_text != ticket["payload_text"]
            or old.context_watermark != ticket["context_watermark"]
            or old.handoff_id != ticket["handoff_id"]
            or self._inputs(source_id) != ticket["source_inputs_json"]
        ):
            raise PreparationRetryRefused(
                "Retry paused: saved session, root, route, permissions or execution evidence "
                "changed. Inspect /status; no new task was started."
            )
        context_refusal = self._context_refusal(source_id, ticket["expected_thread_id"])
        if context_refusal is not None:
            raise PreparationRetryRefused(context_refusal)
        return ticket

    def retry_from_notice(
        self,
        *,
        source_job_id: str,
        chat_id: int,
        thread_id: int,
        notice_message_id: int,
        reply_message_id: int,
        canonical_root: Path,
        model_provider: str | None,
    ) -> tuple[ProviderJobRecord, bool]:
        with self.state._immediate_transaction():
            if (
                self.source_for_notice(
                    chat_id=chat_id, thread_id=thread_id, notice_message_id=notice_message_id
                )
                != source_job_id
            ):
                raise StateError("preparation retry notice binding changed")
            prior = self.connection.execute(
                "SELECT child_job_id FROM provider_preexecution_retries WHERE source_job_id=?",
                (source_job_id,),
            ).fetchone()
            if prior is not None:
                self._control(
                    source_job_id, chat_id, thread_id, notice_message_id, reply_message_id
                )
                return self.state.get_provider_job(prior[0]), False
            ticket = self._validate(
                source_job_id, canonical_root=canonical_root, model_provider=model_provider
            )
            if self.state.message_already_observed(chat_id, reply_message_id):
                raise StateError("preparation retry control was already consumed")
            old = self.state.get_provider_job(source_job_id)
            child, created = ProviderAdmissionState(self.state).admit_in_transaction(
                idempotency_key="preexecution-retry:" + source_job_id,
                chat_id=chat_id,
                message_id=reply_message_id,
                topic_id=old.topic_id,
                agent_id="codex",
                session_id=old.session_id,
                session_generation=old.session_generation,
                model=ticket["model"],
                effort=ticket["effort"],
                payload_text=ticket["payload_text"],
                provider_session_id=ticket["expected_thread_id"],
                context_watermark=ticket["context_watermark"],
                handoff_id=ticket["handoff_id"],
                max_attempts=1,
                prepare_task_notices=True,
                control_input="retry",
                attach_forwarded_materials=False,
            )
            if not created:
                raise StateError("preparation retry child lacks its committed provenance")
            self.connection.execute(
                "INSERT INTO provider_preexecution_retries VALUES (?,?,?)",
                (source_job_id, child.job_id, datetime.now(timezone.utc).isoformat()),
            )
            self._control(source_job_id, chat_id, thread_id, notice_message_id, reply_message_id)
            return child, True

    def _control(self, source: str, chat: int, thread: int, notice: int, reply: int) -> None:
        if reply <= 0:
            raise StateError("invalid retry reply identity")
        existing = self.connection.execute(
            "SELECT * FROM provider_preexecution_retry_controls WHERE chat_id=? AND message_id=?",
            (chat, reply),
        ).fetchone()
        if existing is not None:
            if (
                existing["source_job_id"],
                existing["thread_id"],
                existing["notice_message_id"],
            ) != (source, thread, notice):
                raise StateError("retry control identity belongs to another notice")
            return
        if (
            self.state.message_already_observed(chat, reply)
            and not self.connection.execute(
                "SELECT 1 FROM provider_preexecution_retries retries JOIN provider_jobs child "
                "ON child.job_id=retries.child_job_id WHERE retries.source_job_id=? "
                "AND child.chat_id=? AND child.message_id=?",
                (source, chat, reply),
            ).fetchone()
        ):
            raise StateError("retry control was already observed for another action")
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO provider_preexecution_retry_controls VALUES (?,?,?,?,?,?)",
            (chat, reply, thread, notice, source, now),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO observed_messages VALUES (?,?,'hub',?)",
            (chat, reply, now),
        )

    def require_execution_binding(
        self, job: ProviderJobRecord, *, root: Path, model_provider: str | None
    ) -> None:
        link = self.connection.execute(
            "SELECT source_job_id FROM provider_preexecution_retries WHERE child_job_id=?",
            (job.job_id,),
        ).fetchone()
        if link is None:
            return
        ticket = self._validate(link[0], canonical_root=root, model_provider=model_provider)
        if (
            job.provider_session_id != ticket["expected_thread_id"]
            or job.payload_text != ticket["payload_text"]
            or job.context_watermark != ticket["context_watermark"]
            or job.handoff_id != ticket["handoff_id"]
        ):
            raise PreparationRetryRefused(
                "Retry execution snapshot changed; provider was not started."
            )
