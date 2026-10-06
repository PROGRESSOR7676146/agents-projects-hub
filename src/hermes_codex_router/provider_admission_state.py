"""Provider-input admission under a single HubState-owned transaction."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Sequence

from .incoming_materials import IncomingMaterialDraft
from .provider_queue_capacity import QueueCapacityConfig
from .root_blockers import persistent_root_blocker
from .state_errors import StateError
from .state_provider_jobs import ProviderJobRecord
from .state_sessions import SessionsStateFacade, WriterTransferSnapshot
from .state_values import _bounded, _now, _optional_bounded, _timestamp

if TYPE_CHECKING:
    from .state import HubState


class ProviderAdmissionState:
    def __init__(self, state: HubState) -> None:
        self.state = state

    def admit_in_transaction(
        self,
        *,
        idempotency_key: str,
        chat_id: int,
        message_id: int,
        topic_id: int,
        agent_id: str,
        session_id: str,
        session_generation: int,
        model: str,
        effort: str,
        payload_text: str,
        provider_session_id: str | None = None,
        context_watermark: int | None = None,
        handoff_id: str | None = None,
        materials: Sequence[IncomingMaterialDraft] = (),
        input_group_key: str | None = None,
        max_attempts: int = 5,
        take_local_writer: bool = False,
        available_at: datetime | None = None,
        expected_transfer: WriterTransferSnapshot | None = None,
        prepare_task_notices: bool = False,
        queue_capacity: QueueCapacityConfig | None = None,
        control_input: str | None = None,
        attach_forwarded_materials: bool = True,
    ) -> tuple[ProviderJobRecord, bool]:
        """Admit one input without committing or opening a nested transaction."""

        key = _bounded(idempotency_key, name="idempotency key", maximum=256)
        target_agent = _bounded(agent_id, name="agent id", maximum=64)
        target_session = _bounded(session_id, name="session id", maximum=128)
        selected_model = _bounded(model, name="model", maximum=200)
        selected_effort = _bounded(effort, name="effort", maximum=64)
        payload = _bounded(payload_text, name="payload", maximum=20000)
        requested_provider_session = (
            _bounded(provider_session_id, name="provider session id", maximum=256)
            if provider_session_id is not None
            else None
        )
        handoff = (
            _bounded(handoff_id, name="handoff id", maximum=128) if handoff_id is not None else None
        )
        group_key = _optional_bounded(input_group_key, name="input group key", maximum=256)
        if chat_id == 0 or message_id <= 0 or session_generation <= 0:
            raise StateError("invalid provider job identity")
        if context_watermark is not None and context_watermark < 0:
            raise StateError("invalid context watermark")
        if not 1 <= max_attempts <= 20:
            raise StateError("invalid max attempts")

        if not self.state._connection.in_transaction:
            raise StateError("provider admission requires its owner transaction")
        existing = self.state._connection.execute(
            "SELECT * FROM provider_jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            if int(existing["chat_id"]) != chat_id or int(existing["message_id"]) != message_id:
                raise StateError("idempotency key belongs to another Telegram message")
            return self.state._provider_job(existing), False

        session = self._require_binding_in_transaction(
            topic_id=topic_id,
            chat_id=chat_id,
            target_session=target_session,
            target_agent=target_agent,
            session_generation=session_generation,
            message_id=message_id,
            take_local_writer=take_local_writer,
            selected_model=selected_model,
            selected_effort=selected_effort,
            requested_provider_session=requested_provider_session,
            context_watermark=context_watermark,
            handoff=handoff,
            expected_transfer=expected_transfer,
        )
        persisted_provider_session = session["provider_session_id"]

        now = _now()
        ready_at = _timestamp(available_at) if available_at is not None else None
        self.state._connection.execute(
            """INSERT OR IGNORE INTO observed_messages
               (chat_id, message_id, observer_agent_id, observed_at)
               VALUES (?, ?, 'hub', ?)""",
            (chat_id, message_id, now),
        )
        self.state._connection.execute(
            """INSERT INTO topic_queue_counters(topic_id, next_sequence, updated_at)
               VALUES (?, 1, ?)
               ON CONFLICT(topic_id) DO NOTHING""",
            (topic_id, now),
        )
        counter = self.state._connection.execute(
            "SELECT next_sequence FROM topic_queue_counters WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        if counter is None:
            raise StateError("failed to allocate provider job sequence")
        topic_sequence = int(counter["next_sequence"])
        self.state._connection.execute(
            """UPDATE topic_queue_counters
               SET next_sequence = next_sequence + 1, updated_at = ?
               WHERE topic_id = ?""",
            (now, topic_id),
        )
        job_id = str(uuid.uuid4())
        try:
            self.state._connection.execute(
                """INSERT INTO provider_jobs (
                     job_id, idempotency_key, chat_id, message_id, topic_id,
                     topic_sequence, agent_id, session_id, session_generation,
                     provider_session_id, model, effort, payload_text, codex_permission_profile,
                     context_watermark, handoff_id, input_group_key, status, attempt_count,
                     max_attempts, next_attempt_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             'queued', 0, ?, ?, ?, ?)""",
                (
                    job_id,
                    key,
                    chat_id,
                    message_id,
                    topic_id,
                    topic_sequence,
                    target_agent,
                    target_session,
                    session_generation,
                    persisted_provider_session,
                    selected_model,
                    selected_effort,
                    payload,
                    session["codex_permission_profile"],
                    context_watermark,
                    handoff,
                    group_key,
                    max_attempts,
                    ready_at,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            duplicate = self.state._connection.execute(
                """SELECT * FROM provider_jobs
                   WHERE idempotency_key = ? OR (chat_id = ? AND message_id = ?)""",
                (key, chat_id, message_id),
            ).fetchone()
            if duplicate is None:
                raise
            if str(duplicate["idempotency_key"]) != key:
                raise StateError("Telegram message already has another provider job") from exc
            return self.state._provider_job(duplicate), False
        self.state._connection.execute(
            """INSERT INTO provider_job_inputs (
                   job_id, chat_id, message_id, part_index, input_text, received_at
               ) VALUES (?, ?, ?, 1, ?, ?)""",
            (
                job_id,
                chat_id,
                message_id,
                control_input if control_input is not None else payload,
                now,
            ),
        )
        if attach_forwarded_materials:
            self.state._attach_forwarded_materials(
                job_id=job_id,
                topic_id=topic_id,
                agent_id=target_agent,
                session_id=target_session,
                session_generation=session_generation,
            )
        self.state._insert_incoming_materials(
            job_id=job_id,
            topic_id=topic_id,
            chat_id=chat_id,
            message_id=message_id,
            agent_id=target_agent,
            session_id=target_session,
            session_generation=session_generation,
            materials=materials,
            timestamp=now,
        )
        row = self.state._connection.execute(
            "SELECT * FROM provider_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise StateError("failed to persist provider job")
        job = self.state._provider_job(row)
        if prepare_task_notices:
            self.state.queue_visibility.admitted_in_transaction(
                job_id, now=datetime.fromisoformat(now), capacity=queue_capacity
            )
        return job, True

    def _require_binding_in_transaction(
        self,
        *,
        topic_id: int,
        chat_id: int,
        target_session: str,
        target_agent: str,
        session_generation: int,
        message_id: int,
        take_local_writer: bool,
        selected_model: str,
        selected_effort: str,
        requested_provider_session: str | None,
        context_watermark: int | None,
        handoff: str | None,
        expected_transfer: WriterTransferSnapshot | None,
    ) -> sqlite3.Row:
        """Validate authority/context and perform an explicitly requested writer transfer."""
        from .session_adoption_state import CodexSessionOrigins

        topic = self.state._connection.execute(
            "SELECT chat_id, thread_id FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if topic is None:
            raise StateError(f"unknown topic_id: {topic_id}")
        if int(topic["chat_id"]) != chat_id:
            raise StateError("provider job chat does not match topic")
        session = self.state._connection.execute(
            """SELECT * FROM agent_sessions
               WHERE session_id = ?""",
            (target_session,),
        ).fetchone()
        if (
            session is None
            or int(session["topic_id"]) != topic_id
            or str(session["agent_id"]) != target_agent
            or int(session["generation"]) != session_generation
        ):
            raise StateError("provider job session snapshot does not match persisted session")
        if str(session["status"]) not in {"active", "satellite"}:
            raise StateError("provider job session is not routable")
        self.state._sessions_state.require_codex_selection(SessionsStateFacade.record(session))
        CodexSessionOrigins(self.state).require_admission(target_session, message_id)
        expected_writer = "local" if take_local_writer else "telegram"
        if str(session["writer_mode"]) != expected_writer:
            raise StateError(f"provider job session writer is not {expected_writer}")
        if (
            not take_local_writer
            and persistent_root_blocker(self.state._connection, topic_id=topic_id) is not None
        ):
            raise StateError("execution root has a persistent local writer or uncertainty")
        if str(session["model"]) != selected_model or str(session["effort"]) != selected_effort:
            raise StateError("provider job model or effort does not match persisted session")
        persisted_provider_session = session["provider_session_id"]
        if (
            requested_provider_session is not None
            and requested_provider_session != persisted_provider_session
        ):
            raise StateError("provider job provider session does not match persisted session")
        if context_watermark is not None:
            context_turn = self.state._connection.execute(
                """SELECT 1 FROM external_turn_excerpts
                   WHERE turn_id = ? AND topic_id = ?""",
                (context_watermark, topic_id),
            ).fetchone()
            if context_turn is None:
                raise StateError("provider job context watermark is not a visible turn for topic")
        if handoff is not None:
            pending = self.state._connection.execute(
                """SELECT 1 FROM pending_handoffs
                   WHERE handoff_id = ? AND topic_id = ? AND target_agent_id = ?""",
                (handoff, topic_id, target_agent),
            ).fetchone()
            if pending is None:
                raise StateError("provider job handoff snapshot is not pending")

        if take_local_writer:
            if expected_transfer is None or expected_transfer.session.session_id != target_session:
                raise StateError("local writer transfer requires a validated snapshot")
            self.state._require_writer_transfer_snapshot(expected_transfer)
            pending_job = self.state._connection.execute(
                """SELECT 1 FROM provider_jobs
                   WHERE topic_id = ? AND status IN
                     ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
                   LIMIT 1""",
                (topic_id,),
            ).fetchone()
            if pending_job is not None:
                raise StateError("provider work is already pending for this topic")
            cursor = self.state._connection.execute(
                """UPDATE agent_sessions SET writer_mode = 'telegram', updated_at = ?
                   WHERE session_id = ? AND writer_mode = 'local'""",
                (_now(), target_session),
            )
            if cursor.rowcount != 1:
                raise StateError("local writer ownership changed during provider admission")

        return session
