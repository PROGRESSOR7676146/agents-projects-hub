from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Mapping, TypedDict

if TYPE_CHECKING:
    from .state import TopicRecord


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    topic_id: int
    agent_id: str
    generation: int
    status: str
    model: str
    effort: str
    provider_session_id: str | None
    terminal_name: str | None
    writer_mode: str
    context_remaining_percent: float | None


@dataclass(frozen=True, slots=True)
class WriterTransferSnapshot:
    topic: TopicRecord
    session: SessionRecord
    lane: tuple[tuple[str, object], ...] | None


class TelegramContractProvenance(TypedDict):
    session_id: str
    agent_id: str
    status: str
    provider_bound: bool
    acknowledged_version: int


StateErrorFactory = Callable[[str], Exception]
TransactionFactory = Callable[[], AbstractContextManager[None]]
TopicLookup = Callable[[int], "TopicRecord"]
ActiveLaneLookup = Callable[[int], Mapping[str, object] | None]
TopicBusyCheck = Callable[[int], bool]
OriginExists = Callable[[str], bool]
OriginActivate = Callable[[str, int, int], None]


class SessionsStateFacade:
    """Session lifecycle and writer ownership on the HubState-owned connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: TransactionFactory,
        write_transaction: TransactionFactory,
        state_error: StateErrorFactory,
        get_topic: TopicLookup,
        active_lane_for_topic: ActiveLaneLookup,
        topic_has_running_dispatch: TopicBusyCheck,
        topic_has_pending_provider_job: TopicBusyCheck,
        origin_exists: OriginExists,
        activate_origin: OriginActivate,
    ) -> None:
        self._connection = connection
        self._transaction = transaction
        self._write_transaction = write_transaction
        self._state_error = state_error
        self._get_topic = get_topic
        self._active_lane_for_topic = active_lane_for_topic
        self._topic_has_running_dispatch = topic_has_running_dispatch
        self._topic_has_pending_provider_job = topic_has_pending_provider_job
        self._origin_exists = origin_exists
        self._activate_origin = activate_origin

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _bounded(self, value: str, *, name: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise self._state_error(f"invalid {name}")
        return normalized

    @staticmethod
    def record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            topic_id=row["topic_id"],
            agent_id=row["agent_id"],
            generation=row["generation"],
            status=row["status"],
            model=row["model"],
            effort=row["effort"],
            provider_session_id=row["provider_session_id"],
            terminal_name=row["terminal_name"],
            writer_mode=row["writer_mode"],
            context_remaining_percent=row["context_remaining_percent"],
        )

    def active_session(self, topic_id: int) -> SessionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
            (topic_id,),
        ).fetchone()
        return None if row is None else self.record(row)

    def get_session(self, session_id: str) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise self._state_error(f"unknown session_id: {session_id}")
        return self.record(row)

    def bind_provider_session(
        self, session_id: str, provider_session_id: str, terminal_name: str | None
    ) -> SessionRecord:
        if not provider_session_id.strip():
            raise self._state_error("provider session id is empty")
        with self._write_transaction():
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET provider_session_id = ?, terminal_name = ?, "
                "updated_at = ? WHERE session_id = ?",
                (provider_session_id, terminal_name, self._now(), session_id),
            )
        if cursor.rowcount != 1:
            raise self._state_error(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def writer_transfer_snapshot(
        self, topic: TopicRecord, session: SessionRecord
    ) -> WriterTransferSnapshot:
        with self._transaction():
            snapshot = WriterTransferSnapshot(
                topic, session, self._writer_lane_snapshot(topic.topic_id)
            )
            self._require_writer_transfer_snapshot(snapshot)
            return snapshot

    def _writer_lane_snapshot(self, topic_id: int) -> tuple[tuple[str, object], ...] | None:
        lane = self._active_lane_for_topic(topic_id)
        return None if lane is None else tuple(sorted(lane.items()))

    def _require_writer_transfer_snapshot(self, snapshot: WriterTransferSnapshot) -> None:
        if (
            snapshot.session.topic_id != snapshot.topic.topic_id
            or self._get_topic(snapshot.topic.topic_id) != snapshot.topic
            or self.get_session(snapshot.session.session_id) != snapshot.session
            or self._writer_lane_snapshot(snapshot.topic.topic_id) != snapshot.lane
            or snapshot.session.status != "active"
            or self._topic_has_running_dispatch(snapshot.topic.topic_id)
            or self._topic_has_pending_provider_job(snapshot.topic.topic_id)
        ):
            raise self._state_error("writer transfer snapshot changed; retry the command")

    def set_writer_mode(
        self,
        session_id: str,
        writer_mode: str,
        *,
        expected_transfer: WriterTransferSnapshot | None = None,
    ) -> SessionRecord:
        if writer_mode not in {"telegram", "local", "terminal"}:
            raise self._state_error("invalid writer mode")
        with self._transaction():
            if expected_transfer is not None:
                if expected_transfer.session.session_id != session_id:
                    raise self._state_error("writer transfer session mismatch")
                self._require_writer_transfer_snapshot(expected_transfer)
            session = self._connection.execute(
                """SELECT sessions.session_id, sessions.writer_mode,
                          COALESCE(topics.execution_scope, 'project:' || topics.project_id)
                            AS execution_scope
                   FROM agent_sessions sessions
                   JOIN topics ON topics.topic_id = sessions.topic_id
                   WHERE sessions.session_id = ?""",
                (session_id,),
            ).fetchone()
            if session is None:
                raise self._state_error(f"unknown session_id: {session_id}")
            if writer_mode != "telegram" and session["writer_mode"] == "telegram":
                scope = str(session["execution_scope"])
                conflicting_writer = self._connection.execute(
                    """SELECT 1 FROM agent_sessions other
                       JOIN topics ON topics.topic_id = other.topic_id
                       WHERE other.session_id != ?
                         AND other.status IN ('active', 'satellite')
                         AND other.writer_mode != 'telegram'
                         AND COALESCE(topics.execution_scope,
                                      'project:' || topics.project_id) = ?
                       LIMIT 1""",
                    (session_id, scope),
                ).fetchone()
                conflicting_job = self._connection.execute(
                    """SELECT 1 FROM provider_jobs jobs
                       JOIN topics ON topics.topic_id = jobs.topic_id
                       WHERE COALESCE(topics.execution_scope,
                                      'project:' || topics.project_id) = ?
                         AND (
                           jobs.status IN (
                             'queued', 'leased', 'executing', 'retry_wait', 'result_ready'
                           )
                           OR (jobs.status = 'indeterminate' AND NOT EXISTS (
                             SELECT 1 FROM provider_job_resolutions resolutions
                             WHERE resolutions.job_id = jobs.job_id
                           ))
                         )
                       LIMIT 1""",
                    (scope,),
                ).fetchone()
                conflicting_dispatch = self._connection.execute(
                    """SELECT 1 FROM turn_dispatches dispatches
                       JOIN topics ON topics.topic_id = dispatches.topic_id
                       WHERE dispatches.status = 'running'
                         AND COALESCE(topics.execution_scope,
                                      'project:' || topics.project_id) = ?
                       LIMIT 1""",
                    (scope,),
                ).fetchone()
                if any(
                    conflict is not None
                    for conflict in (conflicting_writer, conflicting_job, conflicting_dispatch)
                ):
                    raise self._state_error("execution root is owned by another writer")
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET writer_mode = ?, updated_at = ? WHERE session_id = ?",
                (writer_mode, self._now(), session_id),
            )
        if cursor.rowcount != 1:
            raise self._state_error(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def return_codex_local_writer(
        self,
        *,
        chat_id: int,
        message_id: int,
        topic_id: int,
        session_id: str,
        observer_agent_id: str,
    ) -> tuple[SessionRecord, bool]:
        observer = self._bounded(observer_agent_id, name="observer agent id", maximum=64)
        if chat_id == 0 or message_id <= 0:
            raise self._state_error("invalid Telegram message identity")
        with self._transaction():
            existing = self._connection.execute(
                "SELECT 1 FROM observed_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            if existing is not None:
                return self.get_session(session_id), False
            topic = self._connection.execute(
                "SELECT chat_id FROM topics WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if topic is None or int(topic["chat_id"]) != chat_id:
                raise self._state_error("Codex return does not match topic")
            session = self._connection.execute(
                """SELECT topic_id, agent_id, status, writer_mode
                   FROM agent_sessions WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            if (
                session is None
                or int(session["topic_id"]) != topic_id
                or str(session["agent_id"]) != "codex"
                or str(session["status"]) != "active"
                or str(session["writer_mode"]) != "local"
            ):
                raise self._state_error("Codex local writer state changed during return")
            running_dispatch = self._connection.execute(
                """SELECT 1 FROM turn_dispatches
                   WHERE topic_id = ? AND status = 'running' LIMIT 1""",
                (topic_id,),
            ).fetchone()
            pending_job = self._connection.execute(
                """SELECT 1 FROM provider_jobs
                   WHERE topic_id = ? AND status IN
                     ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
                   LIMIT 1""",
                (topic_id,),
            ).fetchone()
            if running_dispatch is not None or pending_job is not None:
                raise self._state_error("provider work is already pending for this topic")
            now = self._now()
            cursor = self._connection.execute(
                """UPDATE agent_sessions SET writer_mode = 'telegram', updated_at = ?
                   WHERE session_id = ? AND writer_mode = 'local'""",
                (now, session_id),
            )
            if cursor.rowcount != 1:
                raise self._state_error("Codex local writer ownership changed during return")
            self._activate_origin(session_id, message_id, topic_id)
            self._connection.execute(
                """INSERT INTO observed_messages
                   (chat_id, message_id, observer_agent_id, observed_at)
                   VALUES (?, ?, ?, ?)""",
                (chat_id, message_id, observer, now),
            )
        return self.get_session(session_id), True

    def set_context_remaining(self, session_id: str, percent: float | None) -> SessionRecord:
        bounded = None if percent is None else max(0.0, min(100.0, percent))
        with self._write_transaction():
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET context_remaining_percent = ?, updated_at = ? "
                "WHERE session_id = ?",
                (bounded, self._now(), session_id),
            )
        if cursor.rowcount != 1:
            raise self._state_error(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def telegram_contract_version(self, session_id: str) -> int:
        self.get_session(session_id)
        row = self._connection.execute(
            "SELECT integer_value FROM runtime_checkpoints WHERE checkpoint_key = ?",
            (f"telegram-contract:{session_id}",),
        ).fetchone()
        return 0 if row is None else max(0, int(row["integer_value"]))

    def telegram_contract_provenance(
        self, *, limit: int = 100
    ) -> tuple[TelegramContractProvenance, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("contract provenance limit must be between 1 and 100")
        rows = self._connection.execute(
            """SELECT s.session_id, s.agent_id, s.status,
                      s.provider_session_id,
                      COALESCE(c.integer_value, 0) AS acknowledged_version
               FROM agent_sessions AS s
               LEFT JOIN runtime_checkpoints AS c
                 ON c.checkpoint_key = 'telegram-contract:' || s.session_id
               WHERE s.status IN ('active', 'satellite')
               ORDER BY s.topic_id,
                        CASE s.status WHEN 'active' THEN 0 ELSE 1 END,
                        s.agent_id, s.session_id
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return tuple(
            {
                "session_id": str(row["session_id"]),
                "agent_id": str(row["agent_id"]),
                "status": str(row["status"]),
                "provider_bound": row["provider_session_id"] is not None,
                "acknowledged_version": max(0, int(row["acknowledged_version"])),
            }
            for row in rows
        )

    def acknowledge_telegram_contract(self, session_id: str, version: int) -> None:
        if version <= 0:
            raise self._state_error("Telegram contract version must be positive")
        self.get_session(session_id)
        with self._write_transaction():
            self._connection.execute(
                """INSERT INTO runtime_checkpoints
                   (checkpoint_key, integer_value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(checkpoint_key) DO UPDATE SET
                     integer_value = MAX(integer_value, excluded.integer_value),
                     updated_at = excluded.updated_at""",
                (f"telegram-contract:{session_id}", version, self._now()),
            )

    def _next_generation(self, topic_id: int, agent_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 AS value "
            "FROM agent_sessions WHERE topic_id = ? AND agent_id = ?",
            (topic_id, agent_id),
        ).fetchone()
        return int(row["value"])

    def _insert_session(
        self,
        topic_id: int,
        agent_id: str,
        model: str,
        effort: str,
        status: str,
    ) -> SessionRecord:
        session_id = str(uuid.uuid4())
        generation = self._next_generation(topic_id, agent_id)
        now = self._now()
        self._connection.execute(
            """INSERT INTO agent_sessions
               (session_id, topic_id, agent_id, generation, status, model, effort,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, topic_id, agent_id, generation, status, model, effort, now, now),
        )
        return self.get_session(session_id)

    def _require_control_snapshot(self, topic_id: int, expected_session_id: str | None) -> None:
        if expected_session_id is not None:
            current = self.active_session(topic_id)
            if (current.session_id if current else "") != expected_session_id:
                raise self._state_error("active session changed; open controls again")
            if current is not None and current.writer_mode != "telegram":
                raise self._state_error("return the local writer before changing session settings")
            if self._topic_has_running_dispatch(topic_id) or self._topic_has_pending_provider_job(
                topic_id
            ):
                raise self._state_error(
                    "provider work is pending; retry controls after it completes"
                )

    def activate_agent(
        self,
        topic_id: int,
        agent_id: str,
        model: str,
        effort: str,
        *,
        expected_session_id: str | None = None,
    ) -> SessionRecord:
        self._get_topic(topic_id)
        now = self._now()
        with self._transaction():
            self._require_control_snapshot(topic_id, expected_session_id)
            current = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
                (topic_id,),
            ).fetchone()
            target = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE topic_id = ? AND agent_id = ? "
                "AND status = 'satellite'",
                (topic_id, agent_id),
            ).fetchone()
            if current is not None and current["agent_id"] == agent_id:
                session_id = str(current["session_id"])
            else:
                if current is not None:
                    self._connection.execute(
                        "UPDATE agent_sessions SET status = 'satellite', updated_at = ? "
                        "WHERE session_id = ?",
                        (now, current["session_id"]),
                    )
                if target is not None:
                    session_id = str(target["session_id"])
                    self._connection.execute(
                        "UPDATE agent_sessions SET status = 'active', updated_at = ? "
                        "WHERE session_id = ?",
                        (now, session_id),
                    )
                else:
                    session = self._insert_session(topic_id, agent_id, model, effort, "active")
                    session_id = session.session_id
            self._connection.execute(
                "UPDATE topics SET active_agent_id = ?, updated_at = ? WHERE topic_id = ?",
                (agent_id, now, topic_id),
            )
        return self.get_session(session_id)

    def ensure_satellite(
        self, topic_id: int, agent_id: str, model: str, effort: str
    ) -> SessionRecord:
        topic = self._get_topic(topic_id)
        if topic.active_agent_id == agent_id:
            row = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
                (topic_id,),
            ).fetchone()
            if row is None:
                raise self._state_error("topic has active agent but no active session")
            return self.record(row)
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND agent_id = ? AND status = 'satellite'",
            (topic_id, agent_id),
        ).fetchone()
        if row is not None:
            return self.record(row)
        with self._write_transaction():
            session = self._insert_session(topic_id, agent_id, model, effort, "satellite")
        return self.get_session(session.session_id)

    def new_active_session(
        self, topic_id: int, *, expected_session_id: str | None = None
    ) -> SessionRecord:
        with self._transaction():
            self._require_control_snapshot(topic_id, expected_session_id)
            previous = self.active_session(topic_id)
            if previous is None:
                raise self._state_error("topic has no active session")
            self._connection.execute(
                "UPDATE agent_sessions SET status = 'archived', updated_at = ? WHERE session_id = ?",
                (self._now(), previous.session_id),
            )
            replacement = self._insert_session(
                topic_id, previous.agent_id, previous.model, previous.effort, "active"
            )
        return self.get_session(replacement.session_id)

    def replace_active_session(
        self,
        topic_id: int,
        *,
        model: str,
        effort: str,
        expected_session_id: str | None = None,
    ) -> SessionRecord:
        with self._transaction():
            self._require_control_snapshot(topic_id, expected_session_id)
            previous = self.active_session(topic_id)
            if previous is None:
                raise self._state_error("topic has no active session")
            if self._origin_exists(previous.session_id):
                if previous.writer_mode != "telegram":
                    raise self._state_error(
                        "return the local writer before changing session settings"
                    )
                self._connection.execute(
                    "UPDATE agent_sessions SET model=?, effort=?, updated_at=? WHERE session_id=?",
                    (
                        self._bounded(model, name="model", maximum=200),
                        self._bounded(effort, name="effort", maximum=64),
                        self._now(),
                        previous.session_id,
                    ),
                )
                return self.get_session(previous.session_id)
            self._connection.execute(
                "UPDATE agent_sessions SET status = 'archived', updated_at = ? "
                "WHERE session_id = ?",
                (self._now(), previous.session_id),
            )
            replacement = self._insert_session(topic_id, previous.agent_id, model, effort, "active")
        return self.get_session(replacement.session_id)


__all__ = [
    "SessionRecord",
    "SessionsStateFacade",
    "TelegramContractProvenance",
    "WriterTransferSnapshot",
]
