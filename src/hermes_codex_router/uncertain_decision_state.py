"""State owner for the owner's Telegram decision on an uncertain job (ADR 0107).

Offering and deciding use the HubState connection and its single immediate
transaction. The offer is only a notice part; the decision transaction alone
rechecks the delivered prompt, the job, its session snapshot and the root, and
then appends the resolution, the immutable decision and, for "continue", one
inspection-first job with the root's queued tail held behind it.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import artifact_capabilities
from .delivery_control_predicates import result_ready_control_reconciled
from .uncertain_decision import (
    CONTINUATION_PROMPT,
    PromptAction,
    parse_prompt_part,
    prompt_part_html,
    snapshot_binding,
)

if TYPE_CHECKING:
    from .state import HubState

_DECISIONS = {"continue": "inspect_and_continue", "close": "close_without_retry"}


@dataclass(frozen=True, slots=True)
class UncertainDecision:
    job_id: str
    action: PromptAction
    continuation_job_id: str | None
    held_count: int
    created: bool


class UncertainDecisionState:
    """One HubState-owned transaction per offer or decision."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    def correction_linked(self, job_id: str) -> bool:
        """A streamed-input pair keeps separate owner resolutions; no prompt."""
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='claude_input_children'"
            )
        }
        if not tables:
            return False
        return (
            self.connection.execute(
                "SELECT 1 FROM claude_input_children WHERE parent_job_id=? OR child_job_id=?",
                (job_id, job_id),
            ).fetchone()
            is not None
        )

    def attach_prompt_in_transaction(self, outbox_id: str, job: sqlite3.Row) -> None:
        """Append the decision prompt after every part of the uncertainty notice."""
        from .state import StateError

        if not self.connection.in_transaction:
            raise StateError("uncertain decision prompt requires its notice transaction")
        if not artifact_capabilities.REMOTE_UNCERTAIN_DECISION or job["agent_id"] == "codex":
            return
        session = self.connection.execute(
            "SELECT provider_session_id FROM agent_sessions WHERE session_id=?",
            (job["session_id"],),
        ).fetchone()
        if session is None or self.correction_linked(str(job["job_id"])):
            return
        snapshot = snapshot_binding(
            str(job["job_id"]),
            str(job["session_id"]),
            int(job["session_generation"]),
            session["provider_session_id"],
        )
        index = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(part_index), 0) + 1 FROM telegram_outbox_parts "
                "WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            """INSERT INTO telegram_outbox_parts
               (outbox_id, part_index, telegram_html, part_type, file_path, file_name,
                file_size, file_sha256)
               VALUES (?, ?, ?, 'text', NULL, NULL, NULL, NULL)""",
            (outbox_id, index, prompt_part_html(snapshot)),
        )

    def decide(
        self,
        *,
        action: PromptAction,
        snapshot: str,
        actor_user_id: int,
        callback_id: str,
        chat_id: int,
        thread_id: int,
        prompt_message_id: int,
        canonical_root: Path,
    ) -> UncertainDecision:
        from .codex_control_predicates import require_control_scope_clear
        from .state import StateError

        if not artifact_capabilities.REMOTE_UNCERTAIN_DECISION:
            raise StateError("Решение кнопкой выключено в этой версии Hub")
        root = canonical_root.resolve(strict=True)
        scope = "root:" + str(root)
        now = datetime.now(timezone.utc).isoformat()
        with self.state._immediate_transaction():
            prompt = self.connection.execute(
                """SELECT parts.outbox_id, parts.part_index, parts.telegram_html,
                          outbox.job_id, outbox.thread_id
                   FROM telegram_outbox_parts parts
                   JOIN telegram_outbox outbox ON outbox.outbox_id = parts.outbox_id
                   WHERE outbox.chat_id = ? AND parts.telegram_message_id = ?
                     AND parts.receipt_validation_version = 1 AND parts.part_type = 'text'
                     AND outbox.purpose = 'execution_notice'
                     AND parts.part_index = (SELECT MAX(last.part_index)
                       FROM telegram_outbox_parts last WHERE last.outbox_id = parts.outbox_id)""",
                (chat_id, prompt_message_id),
            ).fetchone()
            if (
                prompt is None
                or int(prompt["thread_id"]) != thread_id
                or parse_prompt_part(str(prompt["telegram_html"])) != snapshot
            ):
                raise StateError("Это не доставленный запрос решения; кнопка не применена")
            job_id = str(prompt["job_id"])
            prior = self.connection.execute(
                "SELECT decision, continuation_job_id FROM provider_job_owner_decisions "
                "WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if prior is not None:
                return UncertainDecision(
                    job_id,
                    "continue" if prior["decision"] == "inspect_and_continue" else "close",
                    prior["continuation_job_id"],
                    0,
                    False,
                )
            job = self.connection.execute(
                """SELECT jobs.*, topics.thread_id, topics.execution_scope,
                          sessions.status AS session_status, sessions.writer_mode,
                          sessions.generation AS current_generation,
                          sessions.provider_session_id AS current_native
                   FROM provider_jobs jobs
                   JOIN topics ON topics.topic_id = jobs.topic_id
                   JOIN agent_sessions sessions ON sessions.session_id = jobs.session_id
                   WHERE jobs.job_id = ?""",
                (job_id,),
            ).fetchone()
            if job is None or job["status"] != "indeterminate" or job["agent_id"] == "codex":
                raise StateError("Задача уже не ждёт решения")
            if (
                self.connection.execute(
                    "SELECT 1 FROM provider_job_resolutions WHERE job_id=?", (job_id,)
                ).fetchone()
                or self.connection.execute(
                    "SELECT 1 FROM provider_turn_terminal_evidence WHERE job_id=?", (job_id,)
                ).fetchone()
            ):
                raise StateError("Задача уже решена другим способом")
            if (
                int(job["chat_id"]) != chat_id
                or int(job["thread_id"]) != thread_id
                or job["session_status"] != "active"
                or job["writer_mode"] != "telegram"
                or int(job["current_generation"]) != int(job["session_generation"])
                or job["execution_scope"] != scope
                or snapshot
                != snapshot_binding(
                    job_id,
                    str(job["session_id"]),
                    int(job["current_generation"]),
                    job["current_native"],
                )
                or self.correction_linked(job_id)
            ):
                raise StateError("Сессия или проект изменились после запроса; кнопки устарели")
            continuation_id: str | None = None
            held = 0
            self.connection.execute(
                "INSERT INTO provider_job_resolutions (job_id, resolution, resolved_at) "
                "VALUES (?, 'acknowledged', ?)",
                (job_id, now),
            )
            if action == "continue":
                try:
                    require_control_scope_clear(self.connection, scope)
                except Exception:
                    raise StateError("Операция управления в проекте ещё не подтверждена") from None
                self._require_root_free(scope, job_id, str(job["session_id"]))
                held = self._hold_tail(scope, job_id, now)
                continuation_id = self._insert_continuation(job, prompt_message_id, now)
            self.connection.execute(
                """INSERT INTO provider_job_owner_decisions (
                     job_id, decision, authority, actor_user_id, callback_id, outbox_id,
                     prompt_part_index, chat_id, thread_id, prompt_message_id, session_id,
                     session_generation, snapshot, continuation_job_id, decided_at
                   ) VALUES (?, ?, 'telegram_owner_callback', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    _DECISIONS[action],
                    actor_user_id,
                    callback_id[:200],
                    prompt["outbox_id"],
                    int(prompt["part_index"]),
                    chat_id,
                    thread_id,
                    prompt_message_id,
                    job["session_id"],
                    int(job["session_generation"]),
                    snapshot,
                    continuation_id,
                    now,
                ),
            )
            self.state._provider_job_state.complete_finished_stops(int(job["topic_id"]), now)
            return UncertainDecision(job_id, action, continuation_id, held, True)

    def _require_root_free(self, scope: str, job_id: str, session_id: str) -> None:
        from .state import StateError

        conflict = self.connection.execute(
            f"""SELECT 1 FROM provider_jobs jobs
               JOIN topics ON topics.topic_id = jobs.topic_id
               WHERE topics.execution_scope = ? AND jobs.job_id != ? AND (
                 (jobs.status IN ('leased', 'executing', 'result_ready')
                  AND NOT {result_ready_control_reconciled("jobs")})
                 OR (jobs.status = 'indeterminate' AND NOT EXISTS (
                   SELECT 1 FROM provider_job_resolutions resolution
                   WHERE resolution.job_id = jobs.job_id
                 ) AND NOT EXISTS (
                   SELECT 1 FROM provider_turn_terminal_evidence terminal
                   WHERE terminal.job_id = jobs.job_id
                 ))
               ) LIMIT 1""",
            (scope, job_id),
        ).fetchone()
        writer = self.connection.execute(
            """SELECT 1 FROM agent_sessions sessions
               JOIN topics ON topics.topic_id = sessions.topic_id
               WHERE topics.execution_scope = ? AND sessions.session_id != ?
                 AND sessions.status IN ('active', 'satellite')
                 AND sessions.writer_mode != 'telegram' LIMIT 1""",
            (scope, session_id),
        ).fetchone()
        dispatch = self.connection.execute(
            """SELECT 1 FROM turn_dispatches dispatches
               JOIN topics ON topics.topic_id = dispatches.topic_id
               WHERE topics.execution_scope = ? AND dispatches.status = 'running' LIMIT 1""",
            (scope,),
        ).fetchone()
        if conflict or writer or dispatch:
            raise StateError("В проекте есть другая активная или неопределённая работа")

    def _hold_tail(self, scope: str, job_id: str, now: str) -> int:
        """Keep earlier accepted work behind the inspection; the owner releases it.

        A queued request whose earlier hold the owner already confirmed cannot
        be held again without reusing that old decision, and it would run before
        the inspection. "Continue" then refuses and rolls back; "close" remains.
        """
        from .state import StateError

        self.connection.execute(
            """INSERT OR IGNORE INTO provider_job_holds (job_id, cause_job_id, held_at)
               SELECT tail.job_id, ?, ? FROM provider_jobs tail
               JOIN topics ON topics.topic_id = tail.topic_id
               WHERE topics.execution_scope = ? AND tail.status IN ('queued', 'retry_wait')""",
            (job_id, now, scope),
        )
        counts = self.connection.execute(
            """SELECT COUNT(*) AS total, COALESCE(SUM(holds.decision = 'pending'), 0) AS held
               FROM provider_jobs jobs
               JOIN topics ON topics.topic_id = jobs.topic_id
               LEFT JOIN provider_job_holds holds ON holds.job_id = jobs.job_id
               WHERE topics.execution_scope = ? AND jobs.status IN ('queued', 'retry_wait')""",
            (scope,),
        ).fetchone()
        if int(counts["held"]) != int(counts["total"]):
            raise StateError(
                "В очереди проекта есть уже подтверждённые запросы; "
                "проверка не встанет перед ними. Выберите «Закрыть без повтора»"
            )
        return int(counts["held"])

    def _insert_continuation(self, job: sqlite3.Row, message_id: int, now: str) -> str:
        from .state import StateError

        counter = self.connection.execute(
            "SELECT next_sequence FROM topic_queue_counters WHERE topic_id = ?",
            (job["topic_id"],),
        ).fetchone()
        if counter is None:
            raise StateError("У темы нет счётчика очереди; продолжение не создано")
        continuation_id = str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO provider_jobs (
                 job_id, idempotency_key, chat_id, message_id, topic_id, topic_sequence,
                 agent_id, session_id, session_generation, provider_session_id,
                 model, effort, payload_text, codex_permission_profile, status, attempt_count,
                 max_attempts, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'queued', 0, 1, ?, ?)""",
            (
                continuation_id,
                "continuation:" + str(job["job_id"]),
                job["chat_id"],
                message_id,
                job["topic_id"],
                counter["next_sequence"],
                job["agent_id"],
                job["session_id"],
                job["session_generation"],
                job["current_native"],
                job["model"],
                job["effort"],
                CONTINUATION_PROMPT,
                now,
                now,
            ),
        )
        self.state.telegram_turn_provenance.record_new_job_in_transaction(continuation_id, None)
        self.connection.execute(
            "UPDATE topic_queue_counters SET next_sequence = next_sequence + 1, "
            "updated_at = ? WHERE topic_id = ?",
            (now, job["topic_id"]),
        )
        self.connection.execute(
            """INSERT INTO provider_job_inputs
               (job_id, chat_id, message_id, part_index, input_text, received_at)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (continuation_id, job["chat_id"], message_id, CONTINUATION_PROMPT, now),
        )
        self.connection.execute(
            """INSERT INTO provider_job_continuations
               (source_job_id, continuation_job_id, chat_id, message_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (job["job_id"], continuation_id, job["chat_id"], message_id, now),
        )
        return continuation_id
