from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .migrations import LATEST_SCHEMA_VERSION, migrate_connection, migrate_database


class StateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TopicRecord:
    topic_id: int
    project_id: str
    chat_id: int
    thread_id: int
    title: str
    active_agent_id: str | None


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
class HandoffRecord:
    handoff_id: str
    topic_id: int
    target_agent_id: str
    source_agent_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ProviderJobRecord:
    job_id: str
    idempotency_key: str
    chat_id: int
    message_id: int
    topic_id: int
    topic_sequence: int
    agent_id: str
    session_id: str
    session_generation: int
    provider_session_id: str | None
    model: str
    effort: str
    payload_text: str
    context_watermark: int | None
    handoff_id: str | None
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: str | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None
    provider_started_at: str | None
    error_class: str | None
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProviderJobResultRecord:
    result_id: str
    job_id: str
    visible_response: str
    provider_session_id: str | None
    actual_model: str | None
    safe_metadata_json: str | None
    context_watermark: int | None
    handoff_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TelegramOutboxRecord:
    outbox_id: str
    job_id: str
    sender_agent_id: str
    chat_id: int
    thread_id: int
    telegram_html: str
    status: str
    attempt_count: int
    available_at: str
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None
    telegram_message_id: int | None
    error_code: str | None
    created_at: str
    updated_at: str
    delivered_at: str | None


@dataclass(frozen=True, slots=True)
class ProviderJobRecovery:
    requeued_job_ids: tuple[str, ...]
    indeterminate_job_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeHealthRecord:
    component: str
    instance_id: str
    runtime: str | None
    agent_id: str | None
    pid: int
    process_start_marker: str
    started_at: str
    heartbeat_at: str
    success_at: str | None
    error_code: str | None
    activity_state: str
    active_job_id: str | None
    active_lease_expires_at: str | None
    provider_state: str
    quota_remaining_percent: float | None
    quota_reset_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class RuntimeHealthStatus:
    status: str
    record: RuntimeHealthRecord | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise StateError("timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat()


def _bounded(value: str, *, name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise StateError(f"invalid {name}")
    return normalized


def _optional_bounded(value: str | None, *, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded(value, name=name, maximum=maximum)


def _parse_timestamp(value: str, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StateError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise StateError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


class HubState:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path) -> "HubState":
        path = path.expanduser().resolve()
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            path.parent.chmod(0o700)
        existed = path.exists() and path.stat().st_size > 0
        if existed:
            probe = sqlite3.connect(path)
            try:
                version = int(probe.execute("PRAGMA user_version").fetchone()[0])
            finally:
                probe.close()
            if version < LATEST_SCHEMA_VERSION:
                migrate_database(path, create_backup=True)
        connection = sqlite3.connect(path, timeout=5.0)
        os.chmod(path, 0o600)
        migrate_connection(connection)
        return cls(connection)

    @property
    def schema_version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        if self._connection.in_transaction:
            raise StateError("cannot nest an immediate state transaction")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @staticmethod
    def _topic(row: sqlite3.Row) -> TopicRecord:
        return TopicRecord(
            topic_id=row["topic_id"],
            project_id=row["project_id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            title=row["title"],
            active_agent_id=row["active_agent_id"],
        )

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
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

    @staticmethod
    def _provider_job(row: sqlite3.Row) -> ProviderJobRecord:
        return ProviderJobRecord(
            job_id=str(row["job_id"]),
            idempotency_key=str(row["idempotency_key"]),
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            topic_id=int(row["topic_id"]),
            topic_sequence=int(row["topic_sequence"]),
            agent_id=str(row["agent_id"]),
            session_id=str(row["session_id"]),
            session_generation=int(row["session_generation"]),
            provider_session_id=row["provider_session_id"],
            model=str(row["model"]),
            effort=str(row["effort"]),
            payload_text=str(row["payload_text"]),
            context_watermark=row["context_watermark"],
            handoff_id=row["handoff_id"],
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=row["next_attempt_at"],
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            provider_started_at=row["provider_started_at"],
            error_class=row["error_class"],
            error_code=row["error_code"],
            error_detail=row["error_detail"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _runtime_health(row: sqlite3.Row) -> RuntimeHealthRecord:
        return RuntimeHealthRecord(
            component=str(row["component"]),
            instance_id=str(row["instance_id"]),
            runtime=None if row["runtime"] is None else str(row["runtime"]),
            agent_id=None if row["agent_id"] is None else str(row["agent_id"]),
            pid=int(row["pid"]),
            process_start_marker=str(row["process_start_marker"]),
            started_at=str(row["started_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            success_at=None if row["success_at"] is None else str(row["success_at"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            activity_state=str(row["activity_state"]),
            active_job_id=(None if row["active_job_id"] is None else str(row["active_job_id"])),
            active_lease_expires_at=(
                None
                if row["active_lease_expires_at"] is None
                else str(row["active_lease_expires_at"])
            ),
            provider_state=str(row["provider_state"]),
            quota_remaining_percent=(
                None
                if row["quota_remaining_percent"] is None
                else float(row["quota_remaining_percent"])
            ),
            quota_reset_at=(None if row["quota_reset_at"] is None else str(row["quota_reset_at"])),
            updated_at=str(row["updated_at"]),
        )

    def upsert_runtime_health(
        self,
        *,
        component: str,
        instance_id: str,
        pid: int,
        process_start_marker: str,
        started_at: datetime,
        heartbeat_at: datetime,
        runtime: str | None = None,
        agent_id: str | None = None,
        success_at: datetime | None = None,
        error_code: str | None = None,
        activity_state: str | None = None,
        active_job_id: str | None = None,
        active_lease_expires_at: datetime | None = None,
        provider_state: str = "unknown",
        quota_remaining_percent: float | None = None,
        quota_reset_at: datetime | None = None,
    ) -> RuntimeHealthRecord:
        """Replace one bounded runtime snapshot without probing its provider."""
        if component not in {"controller", "sender", "provider_worker"}:
            raise StateError("invalid runtime health component")
        instance_id = _bounded(instance_id, name="instance id", maximum=128)
        process_start_marker = _bounded(
            process_start_marker, name="process start marker", maximum=128
        )
        runtime = _optional_bounded(runtime, name="runtime", maximum=64)
        agent_id = _optional_bounded(agent_id, name="agent id", maximum=64)
        error_code = _optional_bounded(error_code, name="error code", maximum=128)
        active_job_id = _optional_bounded(active_job_id, name="active job id", maximum=128)
        if pid <= 0:
            raise StateError("invalid runtime health pid")
        if component == "provider_worker":
            if runtime is None or agent_id is None:
                raise StateError("provider worker health requires runtime and agent id")
        if provider_state not in {"unknown", "ready", "limited", "exhausted", "unavailable"}:
            raise StateError("invalid provider state")
        if component != "provider_worker" and provider_state != "unknown":
            raise StateError("only provider worker health may report provider state")
        if quota_remaining_percent is not None and not 0 <= quota_remaining_percent <= 100:
            raise StateError("invalid quota remaining percent")
        if component != "provider_worker" and (
            quota_remaining_percent is not None or quota_reset_at is not None
        ):
            raise StateError("only provider worker health may report quota state")
        activity_state = activity_state or ("leased" if active_job_id is not None else "idle")
        if activity_state not in {"idle", "leased", "executing", "sending", "unknown"}:
            raise StateError("invalid runtime activity state")
        if active_job_id is None and activity_state not in {"idle", "unknown"}:
            raise StateError("active activity state requires a job id")
        if active_job_id is None and active_lease_expires_at is not None:
            raise StateError("active lease requires a job id")
        started = _timestamp(started_at)
        heartbeat = _timestamp(heartbeat_at)
        success = None if success_at is None else _timestamp(success_at)
        lease_expires = (
            None if active_lease_expires_at is None else _timestamp(active_lease_expires_at)
        )
        quota_reset = None if quota_reset_at is None else _timestamp(quota_reset_at)
        with self._connection:
            self._connection.execute(
                """INSERT INTO runtime_health (
                       component, instance_id, runtime, agent_id, pid, process_start_marker,
                       started_at, heartbeat_at, success_at, error_code, activity_state,
                       active_job_id, active_lease_expires_at, provider_state,
                       quota_remaining_percent, quota_reset_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(component, instance_id) DO UPDATE SET
                     runtime = excluded.runtime,
                     agent_id = excluded.agent_id,
                     pid = excluded.pid,
                     process_start_marker = excluded.process_start_marker,
                     started_at = CASE
                       WHEN runtime_health.process_start_marker = excluded.process_start_marker
                       THEN runtime_health.started_at ELSE excluded.started_at END,
                     heartbeat_at = excluded.heartbeat_at,
                     success_at = excluded.success_at,
                     error_code = excluded.error_code,
                     activity_state = excluded.activity_state,
                     active_job_id = excluded.active_job_id,
                     active_lease_expires_at = excluded.active_lease_expires_at,
                     provider_state = excluded.provider_state,
                     quota_remaining_percent = excluded.quota_remaining_percent,
                     quota_reset_at = excluded.quota_reset_at,
                     updated_at = excluded.updated_at""",
                (
                    component,
                    instance_id,
                    runtime,
                    agent_id,
                    pid,
                    process_start_marker,
                    started,
                    heartbeat,
                    success,
                    error_code,
                    activity_state,
                    active_job_id,
                    lease_expires,
                    provider_state,
                    quota_remaining_percent,
                    quota_reset,
                    heartbeat,
                ),
            )
        record = self.get_runtime_health(component, instance_id)
        if record is None:
            raise StateError("failed to persist runtime health")
        return record

    def get_runtime_health(self, component: str, instance_id: str) -> RuntimeHealthRecord | None:
        row = self._connection.execute(
            "SELECT * FROM runtime_health WHERE component = ? AND instance_id = ?",
            (component, instance_id),
        ).fetchone()
        return None if row is None else self._runtime_health(row)

    def list_runtime_health(self) -> tuple[RuntimeHealthRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM runtime_health ORDER BY component, instance_id"
        ).fetchall()
        return tuple(self._runtime_health(row) for row in rows)

    def runtime_health_status(
        self,
        component: str,
        instance_id: str,
        *,
        now: datetime | None = None,
        degraded_after: timedelta = timedelta(seconds=60),
        stale_after: timedelta = timedelta(minutes=3),
    ) -> RuntimeHealthStatus:
        """Classify a cached heartbeat; this method performs no runtime probe."""
        if degraded_after.total_seconds() <= 0 or stale_after <= degraded_after:
            raise StateError("invalid runtime health staleness thresholds")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise StateError("runtime health classification time must be timezone-aware")
        record = self.get_runtime_health(component, instance_id)
        if record is None:
            return RuntimeHealthStatus("unknown", None)
        heartbeat = _parse_timestamp(record.heartbeat_at, name="runtime heartbeat")
        age = current.astimezone(timezone.utc) - heartbeat
        if age > stale_after:
            status = "stale"
        elif age > degraded_after:
            status = "degraded"
        elif record.error_code is not None or record.provider_state in {
            "limited",
            "exhausted",
            "unavailable",
        }:
            status = "degraded"
        else:
            status = "healthy"
        return RuntimeHealthStatus(status, record)

    @staticmethod
    def _provider_result(row: sqlite3.Row) -> ProviderJobResultRecord:
        return ProviderJobResultRecord(
            result_id=str(row["result_id"]),
            job_id=str(row["job_id"]),
            visible_response=str(row["visible_response"]),
            provider_session_id=row["provider_session_id"],
            actual_model=row["actual_model"],
            safe_metadata_json=row["safe_metadata_json"],
            context_watermark=row["context_watermark"],
            handoff_id=row["handoff_id"],
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _telegram_outbox(row: sqlite3.Row) -> TelegramOutboxRecord:
        return TelegramOutboxRecord(
            outbox_id=str(row["outbox_id"]),
            job_id=str(row["job_id"]),
            sender_agent_id=str(row["sender_agent_id"]),
            chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]),
            telegram_html=str(row["telegram_html"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            available_at=str(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            telegram_message_id=row["telegram_message_id"],
            error_code=row["error_code"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            delivered_at=row["delivered_at"],
        )

    def observe_topic(
        self,
        *,
        project_id: str,
        chat_id: int,
        thread_id: int,
        title: str,
    ) -> TopicRecord:
        # Supergroups use negative IDs; direct bot chats use the positive user
        # ID. Zero is never a valid Telegram chat identity.
        if chat_id == 0 or thread_id <= 0 or not title.strip():
            raise StateError("invalid Telegram topic identity")
        now = _now()
        with self._connection:
            existing = self._connection.execute(
                "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ?",
                (chat_id, thread_id),
            ).fetchone()
            if existing is not None and existing["project_id"] != project_id:
                raise StateError("Telegram topic is already bound to another project")
            if existing is None:
                self._connection.execute(
                    """INSERT INTO topics
                       (project_id, chat_id, thread_id, title, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (project_id, chat_id, thread_id, title.strip(), now, now),
                )
            else:
                self._connection.execute(
                    "UPDATE topics SET title = ?, updated_at = ? WHERE topic_id = ?",
                    (title.strip(), now, existing["topic_id"]),
                )
        row = self._connection.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        if row is None:
            raise StateError("failed to persist Telegram topic")
        return self._topic(row)

    def get_topic(self, topic_id: int) -> TopicRecord:
        row = self._connection.execute(
            "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown topic_id: {topic_id}")
        return self._topic(row)

    def find_topic(self, chat_id: int, thread_id: int) -> TopicRecord | None:
        row = self._connection.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        return None if row is None else self._topic(row)

    def active_agent_for_route(self, chat_id: int, thread_id: int) -> str | None:
        """Return the selected agent for an exact Telegram topic.

        External adapters use this narrow lookup as an admission decision. An
        unknown topic or a topic without an active agent fails closed.
        """
        row = self._connection.execute(
            "SELECT active_agent_id FROM topics WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        if row is None or not row["active_agent_id"]:
            return None
        return str(row["active_agent_id"])

    def active_session(self, topic_id: int) -> SessionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
            (topic_id,),
        ).fetchone()
        return None if row is None else self._session(row)

    def get_session(self, session_id: str) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown session_id: {session_id}")
        return self._session(row)

    def bind_provider_session(
        self, session_id: str, provider_session_id: str, terminal_name: str | None
    ) -> SessionRecord:
        if not provider_session_id.strip():
            raise StateError("provider session id is empty")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET provider_session_id = ?, terminal_name = ?, "
                "updated_at = ? WHERE session_id = ?",
                (provider_session_id, terminal_name, _now(), session_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def set_writer_mode(self, session_id: str, writer_mode: str) -> SessionRecord:
        if writer_mode not in {"telegram", "local", "terminal"}:
            raise StateError("invalid writer mode")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET writer_mode = ?, updated_at = ? WHERE session_id = ?",
                (writer_mode, _now(), session_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def set_context_remaining(self, session_id: str, percent: float) -> SessionRecord:
        bounded = max(0.0, min(100.0, percent))
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET context_remaining_percent = ?, updated_at = ? "
                "WHERE session_id = ?",
                (bounded, _now(), session_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def topic_has_running_dispatch(self, topic_id: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM turn_dispatches WHERE topic_id = ? AND status = 'running' LIMIT 1",
            (topic_id,),
        ).fetchone()
        return row is not None

    def topic_has_pending_provider_job(self, topic_id: int) -> bool:
        """Whether durable work still owns this topic's provider writer."""
        row = self._connection.execute(
            """SELECT 1 FROM provider_jobs
               WHERE topic_id = ?
                 AND status IN ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
               LIMIT 1""",
            (topic_id,),
        ).fetchone()
        return row is not None

    def enqueue_provider_job(
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
        max_attempts: int = 5,
        take_local_writer: bool = False,
    ) -> tuple[ProviderJobRecord, bool]:
        """Atomically accept one bounded provider request.

        This method is deliberately local-only: callers must finish Telegram
        admission and content redaction before calling it. It performs no
        network or provider operation. A duplicate idempotency key returns the
        original immutable snapshot without allocating another topic sequence.
        """
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
        if chat_id == 0 or message_id <= 0 or session_generation <= 0:
            raise StateError("invalid provider job identity")
        if context_watermark is not None and context_watermark < 0:
            raise StateError("invalid context watermark")
        if not 1 <= max_attempts <= 20:
            raise StateError("invalid max attempts")

        created = False
        with self._immediate_transaction():
            existing = self._connection.execute(
                "SELECT * FROM provider_jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                if int(existing["chat_id"]) != chat_id or int(existing["message_id"]) != message_id:
                    raise StateError("idempotency key belongs to another Telegram message")
                return self._provider_job(existing), False

            topic = self._connection.execute(
                "SELECT chat_id, thread_id FROM topics WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if topic is None:
                raise StateError(f"unknown topic_id: {topic_id}")
            if int(topic["chat_id"]) != chat_id:
                raise StateError("provider job chat does not match topic")
            session = self._connection.execute(
                """SELECT topic_id, agent_id, generation, status, writer_mode, model, effort,
                          provider_session_id
                   FROM agent_sessions
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
            expected_writer = "local" if take_local_writer else "telegram"
            if str(session["writer_mode"]) != expected_writer:
                raise StateError(f"provider job session writer is not {expected_writer}")
            if str(session["model"]) != selected_model or str(session["effort"]) != selected_effort:
                raise StateError("provider job model or effort does not match persisted session")
            persisted_provider_session = session["provider_session_id"]
            if (
                requested_provider_session is not None
                and requested_provider_session != persisted_provider_session
            ):
                raise StateError("provider job provider session does not match persisted session")
            if context_watermark is not None:
                context_turn = self._connection.execute(
                    """SELECT 1 FROM external_turn_excerpts
                       WHERE turn_id = ? AND topic_id = ?""",
                    (context_watermark, topic_id),
                ).fetchone()
                if context_turn is None:
                    raise StateError(
                        "provider job context watermark is not a visible turn for topic"
                    )
            if handoff is not None:
                pending = self._connection.execute(
                    """SELECT 1 FROM pending_handoffs
                       WHERE handoff_id = ? AND topic_id = ? AND target_agent_id = ?""",
                    (handoff, topic_id, target_agent),
                ).fetchone()
                if pending is None:
                    raise StateError("provider job handoff snapshot is not pending")

            if take_local_writer:
                pending_job = self._connection.execute(
                    """SELECT 1 FROM provider_jobs
                       WHERE topic_id = ? AND status IN
                         ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
                       LIMIT 1""",
                    (topic_id,),
                ).fetchone()
                if pending_job is not None:
                    raise StateError("provider work is already pending for this topic")
                cursor = self._connection.execute(
                    """UPDATE agent_sessions SET writer_mode = 'telegram', updated_at = ?
                       WHERE session_id = ? AND writer_mode = 'local'""",
                    (_now(), target_session),
                )
                if cursor.rowcount != 1:
                    raise StateError("local writer ownership changed during provider admission")

            now = _now()
            self._connection.execute(
                """INSERT OR IGNORE INTO observed_messages
                   (chat_id, message_id, observer_agent_id, observed_at)
                   VALUES (?, ?, 'hub', ?)""",
                (chat_id, message_id, now),
            )
            self._connection.execute(
                """INSERT INTO topic_queue_counters(topic_id, next_sequence, updated_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(topic_id) DO NOTHING""",
                (topic_id, now),
            )
            counter = self._connection.execute(
                "SELECT next_sequence FROM topic_queue_counters WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
            if counter is None:
                raise StateError("failed to allocate provider job sequence")
            topic_sequence = int(counter["next_sequence"])
            self._connection.execute(
                """UPDATE topic_queue_counters
                   SET next_sequence = next_sequence + 1, updated_at = ?
                   WHERE topic_id = ?""",
                (now, topic_id),
            )
            job_id = str(uuid.uuid4())
            try:
                self._connection.execute(
                    """INSERT INTO provider_jobs (
                         job_id, idempotency_key, chat_id, message_id, topic_id,
                         topic_sequence, agent_id, session_id, session_generation,
                         provider_session_id, model, effort, payload_text,
                         context_watermark, handoff_id, status, attempt_count,
                         max_attempts, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'queued', 0, ?, ?, ?)""",
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
                        context_watermark,
                        handoff,
                        max_attempts,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                duplicate = self._connection.execute(
                    """SELECT * FROM provider_jobs
                       WHERE idempotency_key = ? OR (chat_id = ? AND message_id = ?)""",
                    (key, chat_id, message_id),
                ).fetchone()
                if duplicate is None:
                    raise
                if str(duplicate["idempotency_key"]) != key:
                    raise StateError("Telegram message already has another provider job") from exc
                return self._provider_job(duplicate), False
            created = True
            row = self._connection.execute(
                "SELECT * FROM provider_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise StateError("failed to persist provider job")
            job = self._provider_job(row)
        return job, created

    def get_provider_job(self, job_id: str) -> ProviderJobRecord:
        row = self._connection.execute(
            "SELECT * FROM provider_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown provider job: {job_id}")
        return self._provider_job(row)

    def provider_jobs_for_topic(self, topic_id: int) -> tuple[ProviderJobRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM provider_jobs WHERE topic_id = ? ORDER BY topic_sequence",
            (topic_id,),
        ).fetchall()
        return tuple(self._provider_job(row) for row in rows)

    def lease_provider_job(
        self,
        agent_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> ProviderJobRecord | None:
        target_agent = _bounded(agent_id, name="agent id", maximum=64)
        worker = _bounded(worker_id, name="worker id", maximum=128)
        if not 1 <= lease_seconds <= 3600:
            raise StateError("invalid provider lease duration")
        current = now or datetime.now(timezone.utc)
        timestamp = _timestamp(current)
        expires_at = _timestamp(current + timedelta(seconds=lease_seconds))
        with self._immediate_transaction():
            row = self._connection.execute(
                """SELECT candidate.* FROM provider_jobs candidate
                   WHERE candidate.agent_id = ?
                     AND candidate.attempt_count < candidate.max_attempts
                     AND (
                       candidate.status = 'queued'
                       OR (candidate.status = 'retry_wait'
                           AND candidate.next_attempt_at IS NOT NULL
                           AND candidate.next_attempt_at <= ?)
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM provider_jobs earlier
                       WHERE earlier.topic_id = candidate.topic_id
                         AND earlier.topic_sequence < candidate.topic_sequence
                         AND earlier.status NOT IN ('completed', 'failed', 'cancelled')
                     )
                   ORDER BY candidate.created_at, candidate.topic_id,
                            candidate.topic_sequence
                   LIMIT 1""",
                (target_agent, timestamp),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'leased', lease_owner = ?, lease_token = ?,
                       lease_expires_at = ?, next_attempt_at = NULL,
                       error_class = NULL, error_code = NULL, error_detail = NULL,
                       updated_at = ?
                   WHERE job_id = ? AND status IN ('queued', 'retry_wait')""",
                (worker, token, expires_at, timestamp, row["job_id"]),
            )
            if cursor.rowcount != 1:
                raise StateError("provider job lease race")
            leased = self._connection.execute(
                "SELECT * FROM provider_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            if leased is None:
                raise StateError("leased provider job disappeared")
            return self._provider_job(leased)

    def mark_provider_job_executing(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        timestamp = _timestamp(now)
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'executing', attempt_count = attempt_count + 1,
                       provider_started_at = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'leased' AND lease_token = ?
                     AND lease_expires_at > ? AND attempt_count < max_attempts""",
                (timestamp, timestamp, job_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise StateError("provider job lease is missing, expired, or invalid")
        return self.get_provider_job(job_id)

    def heartbeat_provider_job(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        if not 1 <= lease_seconds <= 3600:
            raise StateError("invalid provider lease duration")
        current = now or datetime.now(timezone.utc)
        timestamp = _timestamp(current)
        expires_at = _timestamp(current + timedelta(seconds=lease_seconds))
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE provider_jobs SET lease_expires_at = ?, updated_at = ?
                   WHERE job_id = ? AND status IN ('leased', 'executing')
                     AND lease_token = ? AND lease_expires_at > ?""",
                (expires_at, timestamp, job_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise StateError("provider job lease is missing, expired, or invalid")
        return self.get_provider_job(job_id)

    def schedule_provider_job_retry(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: int,
        error_detail: str | None = None,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        """Schedule only work which is still provably pre-execution."""
        code = _bounded(error_code, name="error code", maximum=128)
        if not 0 <= delay_seconds <= 86400:
            raise StateError("invalid retry delay")
        detail = error_detail.strip()[:1000] if error_detail else None
        current = now or datetime.now(timezone.utc)
        timestamp = _timestamp(current)
        available_at = _timestamp(current + timedelta(seconds=delay_seconds))
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = CASE
                         WHEN attempt_count + 1 >= max_attempts THEN 'failed'
                         ELSE 'retry_wait'
                       END,
                       attempt_count = attempt_count + 1,
                       next_attempt_at = CASE
                         WHEN attempt_count + 1 >= max_attempts THEN NULL
                         ELSE ?
                       END,
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       error_class = 'transient_pre_execution', error_code = ?,
                       error_detail = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'leased' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (available_at, code, detail, timestamp, job_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise StateError("retry requires a current pre-execution provider job lease")
        return self.get_provider_job(job_id)

    def fail_provider_job(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_code: str,
        error_detail: str | None = None,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        failure_class = _bounded(error_class, name="error class", maximum=64)
        code = _bounded(error_code, name="error code", maximum=128)
        detail = error_detail.strip()[:1000] if error_detail else None
        timestamp = _timestamp(now)
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, error_class = ?, error_code = ?,
                       error_detail = ?, updated_at = ?
                   WHERE job_id = ? AND status IN ('leased', 'executing')
                     AND lease_token = ? AND lease_expires_at > ?""",
                (failure_class, code, detail, timestamp, job_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise StateError("provider job lease is missing or invalid")
        return self.get_provider_job(job_id)

    def mark_provider_job_indeterminate(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        error_detail: str | None = None,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        """Record that a provider invocation might have happened; never retry it."""
        code = _bounded(error_code, name="error code", maximum=128)
        detail = error_detail.strip()[:1000] if error_detail else None
        timestamp = _timestamp(now)
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'indeterminate', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, error_class = 'ambiguous_execution',
                       error_code = ?, error_detail = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'executing' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (code, detail, timestamp, job_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise StateError("provider job lease is missing or invalid")
        return self.get_provider_job(job_id)

    def cancel_provider_job(self, job_id: str) -> ProviderJobRecord:
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'cancelled', next_attempt_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status IN ('queued', 'retry_wait')""",
                (_now(), job_id),
            )
        if cursor.rowcount != 1:
            raise StateError("only queued provider work can be cancelled")
        return self.get_provider_job(job_id)

    def recover_stale_provider_jobs(
        self, *, agent_id: str | None = None, now: datetime | None = None
    ) -> ProviderJobRecovery:
        target_agent = _bounded(agent_id, name="agent id", maximum=64) if agent_id else None
        timestamp = _timestamp(now)
        scope = "AND agent_id = ?" if target_agent is not None else ""
        params: tuple[object, ...] = (timestamp,)
        if target_agent is not None:
            params += (target_agent,)
        with self._immediate_transaction():
            leased = self._connection.execute(
                "SELECT job_id FROM provider_jobs "
                "WHERE status = 'leased' AND lease_expires_at <= ? " + scope + " ORDER BY job_id",
                params,
            ).fetchall()
            executing = self._connection.execute(
                "SELECT job_id FROM provider_jobs "
                "WHERE status = 'executing' AND lease_expires_at <= ? "
                + scope
                + " ORDER BY job_id",
                params,
            ).fetchall()
            self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'queued', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, next_attempt_at = NULL,
                       error_class = 'recovered_pre_execution',
                       error_code = 'stale_lease', updated_at = ?
                   WHERE status = 'leased' AND lease_expires_at <= ? """
                + scope,
                (timestamp, timestamp) + ((target_agent,) if target_agent is not None else ()),
            )
            self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'indeterminate', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, error_class = 'ambiguous_execution',
                       error_code = 'stale_executing_lease', updated_at = ?
                   WHERE status = 'executing' AND lease_expires_at <= ? """
                + scope,
                (timestamp, timestamp) + ((target_agent,) if target_agent is not None else ()),
            )
        return ProviderJobRecovery(
            requeued_job_ids=tuple(str(row["job_id"]) for row in leased),
            indeterminate_job_ids=tuple(str(row["job_id"]) for row in executing),
        )

    def commit_provider_result(
        self,
        job_id: str,
        lease_token: str,
        *,
        visible_response: str,
        sender_agent_id: str,
        telegram_html: str,
        provider_session_id: str | None = None,
        actual_model: str | None = None,
        safe_metadata_json: str | None = None,
        user_excerpt: str | None = None,
        acknowledge_context: bool = False,
        acknowledge_handoff: bool = False,
        now: datetime | None = None,
    ) -> ProviderJobResultRecord:
        """Commit result, acknowledgements, and outbox without a network call."""
        response = _bounded(visible_response, name="visible response", maximum=12000)
        sender = _bounded(sender_agent_id, name="sender agent id", maximum=64)
        html = _bounded(telegram_html, name="Telegram outbox text", maximum=12000)
        provider_session = (
            _bounded(provider_session_id, name="provider session id", maximum=256)
            if provider_session_id is not None
            else None
        )
        selected_model = (
            _bounded(actual_model, name="actual model", maximum=200)
            if actual_model is not None
            else None
        )
        metadata = safe_metadata_json.strip() if safe_metadata_json else None
        if metadata is not None and len(metadata) > 4000:
            raise StateError("invalid safe metadata")
        excerpt = (
            _bounded(user_excerpt, name="user excerpt", maximum=2000)
            if user_excerpt is not None
            else None
        )
        timestamp = _timestamp(now)
        with self._immediate_transaction():
            job_row = self._connection.execute(
                """SELECT jobs.*, topics.thread_id FROM provider_jobs jobs
                   JOIN topics ON topics.topic_id = jobs.topic_id
                   WHERE jobs.job_id = ? AND jobs.status = 'executing'
                     AND jobs.lease_token = ? AND jobs.lease_expires_at > ?""",
                (job_id, lease_token, timestamp),
            ).fetchone()
            if job_row is None:
                raise StateError("provider job lease is missing or invalid")
            if sender != str(job_row["agent_id"]):
                raise StateError("Telegram outbox sender does not match provider job agent")
            result_id = str(uuid.uuid4())
            self._connection.execute(
                """INSERT INTO provider_job_results (
                     result_id, job_id, visible_response, provider_session_id,
                     actual_model, safe_metadata_json, context_watermark,
                     handoff_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result_id,
                    job_id,
                    response,
                    provider_session,
                    selected_model,
                    metadata,
                    job_row["context_watermark"],
                    job_row["handoff_id"],
                    timestamp,
                ),
            )
            if provider_session is not None:
                cursor = self._connection.execute(
                    """UPDATE agent_sessions
                       SET provider_session_id = ?, updated_at = ?
                       WHERE session_id = ? AND topic_id = ? AND agent_id = ?
                         AND generation = ?""",
                    (
                        provider_session,
                        timestamp,
                        job_row["session_id"],
                        job_row["topic_id"],
                        job_row["agent_id"],
                        job_row["session_generation"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateError("provider job session generation changed")
            visible_user_excerpt = excerpt or str(job_row["payload_text"])
            self._connection.execute(
                """INSERT INTO external_turn_excerpts
                   (topic_id, agent_id, provider_session_id, model, provider,
                    user_excerpt, response_excerpt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_row["topic_id"],
                    job_row["agent_id"],
                    provider_session or job_row["provider_session_id"] or "",
                    selected_model or job_row["model"],
                    job_row["agent_id"],
                    visible_user_excerpt,
                    response,
                    timestamp,
                ),
            )
            self._connection.execute(
                """DELETE FROM external_turn_excerpts
                   WHERE topic_id = ? AND turn_id NOT IN (
                     SELECT turn_id FROM external_turn_excerpts
                     WHERE topic_id = ? ORDER BY turn_id DESC LIMIT 100
                   )""",
                (job_row["topic_id"], job_row["topic_id"]),
            )
            if acknowledge_context and job_row["context_watermark"] is not None:
                self._connection.execute(
                    """INSERT INTO visible_context_cursors
                       (topic_id, observer_agent_id, last_turn_id, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(topic_id, observer_agent_id) DO UPDATE SET
                         last_turn_id = MAX(last_turn_id, excluded.last_turn_id),
                         updated_at = excluded.updated_at""",
                    (
                        job_row["topic_id"],
                        job_row["agent_id"],
                        job_row["context_watermark"],
                        timestamp,
                    ),
                )
            if acknowledge_handoff and job_row["handoff_id"] is not None:
                self._connection.execute(
                    "DELETE FROM pending_handoffs WHERE handoff_id = ?",
                    (job_row["handoff_id"],),
                )
            outbox_id = str(uuid.uuid4())
            self._connection.execute(
                """INSERT INTO telegram_outbox (
                     outbox_id, job_id, sender_agent_id, chat_id, thread_id,
                     telegram_html, status, available_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    outbox_id,
                    job_id,
                    sender,
                    job_row["chat_id"],
                    job_row["thread_id"],
                    html,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'result_ready', lease_owner = NULL,
                       lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status = 'executing' AND lease_token = ?""",
                (timestamp, job_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise StateError("provider job lease changed during result commit")
            result_row = self._connection.execute(
                "SELECT * FROM provider_job_results WHERE result_id = ?", (result_id,)
            ).fetchone()
            if result_row is None:
                raise StateError("provider result disappeared")
            result = self._provider_result(result_row)
        return result

    def get_provider_result(self, job_id: str) -> ProviderJobResultRecord:
        row = self._connection.execute(
            "SELECT * FROM provider_job_results WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"provider job has no result: {job_id}")
        return self._provider_result(row)

    def get_telegram_outbox(self, outbox_id: str) -> TelegramOutboxRecord:
        row = self._connection.execute(
            "SELECT * FROM telegram_outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown Telegram outbox row: {outbox_id}")
        return self._telegram_outbox(row)

    def get_telegram_outbox_for_job(self, job_id: str) -> TelegramOutboxRecord:
        row = self._connection.execute(
            "SELECT * FROM telegram_outbox WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"provider job has no Telegram outbox row: {job_id}")
        return self._telegram_outbox(row)

    def lease_telegram_outbox(
        self,
        sender_agent_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord | None:
        sender = _bounded(sender_agent_id, name="sender agent id", maximum=64)
        worker = _bounded(worker_id, name="worker id", maximum=128)
        if not 1 <= lease_seconds <= 3600:
            raise StateError("invalid Telegram outbox lease duration")
        current = now or datetime.now(timezone.utc)
        timestamp = _timestamp(current)
        expires_at = _timestamp(current + timedelta(seconds=lease_seconds))
        with self._immediate_transaction():
            row = self._connection.execute(
                """SELECT outbox.* FROM telegram_outbox outbox
                   JOIN provider_jobs job ON job.job_id = outbox.job_id
                   WHERE outbox.sender_agent_id = ? AND outbox.status = 'pending'
                     AND outbox.available_at <= ? AND outbox.attempt_count < 20
                     AND NOT EXISTS (
                       SELECT 1 FROM telegram_outbox earlier_outbox
                       JOIN provider_jobs earlier_job
                         ON earlier_job.job_id = earlier_outbox.job_id
                       WHERE earlier_job.topic_id = job.topic_id
                         AND earlier_job.topic_sequence < job.topic_sequence
                         AND earlier_outbox.status NOT IN ('delivered', 'failed')
                     )
                   ORDER BY outbox.created_at, outbox.outbox_id LIMIT 1""",
                (sender, timestamp),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            cursor = self._connection.execute(
                """UPDATE telegram_outbox
                   SET status = 'sending', attempt_count = attempt_count + 1,
                       lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                       error_code = NULL, updated_at = ?
                   WHERE outbox_id = ? AND status = 'pending'""",
                (worker, token, expires_at, timestamp, row["outbox_id"]),
            )
            if cursor.rowcount != 1:
                raise StateError("Telegram outbox lease race")
            leased = self._connection.execute(
                "SELECT * FROM telegram_outbox WHERE outbox_id = ?", (row["outbox_id"],)
            ).fetchone()
            if leased is None:
                raise StateError("leased Telegram outbox row disappeared")
            return self._telegram_outbox(leased)

    def heartbeat_telegram_outbox(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord:
        if not 1 <= lease_seconds <= 3600:
            raise StateError("invalid Telegram outbox lease duration")
        current = now or datetime.now(timezone.utc)
        timestamp = _timestamp(current)
        expires_at = _timestamp(current + timedelta(seconds=lease_seconds))
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE telegram_outbox SET lease_expires_at = ?, updated_at = ?
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (expires_at, timestamp, outbox_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise StateError("Telegram outbox lease is missing, expired, or invalid")
        return self.get_telegram_outbox(outbox_id)

    def retry_telegram_outbox(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord:
        code = _bounded(error_code, name="error code", maximum=128)
        if not 0 <= delay_seconds <= 86400:
            raise StateError("invalid Telegram outbox retry delay")
        current = now or datetime.now(timezone.utc)
        timestamp = _timestamp(current)
        available_at = _timestamp(current + timedelta(seconds=delay_seconds))
        with self._immediate_transaction():
            row = self._connection.execute(
                """SELECT job_id, attempt_count FROM telegram_outbox
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (outbox_id, lease_token, timestamp),
            ).fetchone()
            if row is None:
                raise StateError("Telegram outbox lease is missing, expired, or invalid")
            cursor = self._connection.execute(
                """UPDATE telegram_outbox
                   SET status = CASE WHEN attempt_count >= 20 THEN 'failed' ELSE 'pending' END,
                       available_at = ?, lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, error_code = ?, updated_at = ?
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (available_at, code, timestamp, outbox_id, lease_token, timestamp),
            )
            if cursor.rowcount != 1:
                raise StateError("Telegram outbox lease is missing, expired, or invalid")
            if int(row["attempt_count"]) >= 20:
                self._connection.execute(
                    """UPDATE provider_jobs
                       SET status = 'failed', error_class = 'telegram_delivery',
                           error_code = ?, error_detail = NULL, updated_at = ?
                       WHERE job_id = ? AND status = 'result_ready'""",
                    (code, timestamp, row["job_id"]),
                )
        return self.get_telegram_outbox(outbox_id)

    def mark_telegram_outbox_delivered(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        telegram_message_id: int,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord:
        if telegram_message_id <= 0:
            raise StateError("invalid Telegram message id")
        timestamp = _timestamp(now)
        with self._immediate_transaction():
            row = self._connection.execute(
                """SELECT job_id FROM telegram_outbox
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (outbox_id, lease_token, timestamp),
            ).fetchone()
            if row is None:
                raise StateError("Telegram outbox lease is missing or invalid")
            self._connection.execute(
                """UPDATE telegram_outbox
                   SET status = 'delivered', telegram_message_id = ?, delivered_at = ?,
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       updated_at = ? WHERE outbox_id = ? AND status = 'sending'
                         AND lease_token = ? AND lease_expires_at > ?""",
                (telegram_message_id, timestamp, timestamp, outbox_id, lease_token, timestamp),
            )
            cursor = self._connection.execute(
                """UPDATE provider_jobs SET status = 'completed', updated_at = ?
                   WHERE job_id = ? AND status = 'result_ready'""",
                (timestamp, row["job_id"]),
            )
            if cursor.rowcount != 1:
                raise StateError("provider job is not ready for Telegram completion")
            delivered = self._connection.execute(
                "SELECT * FROM telegram_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if delivered is None:
                raise StateError("delivered Telegram outbox row disappeared")
            result = self._telegram_outbox(delivered)
        return result

    def recover_stale_telegram_outbox(
        self,
        *,
        sender_agent_ids: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        timestamp = _timestamp(now)
        agent_filter = ""
        parameters: tuple[object, ...] = (timestamp,)
        if sender_agent_ids is not None:
            if not sender_agent_ids:
                return ()
            placeholders = ", ".join("?" for _ in sender_agent_ids)
            agent_filter = f" AND sender_agent_id IN ({placeholders})"
            parameters = (timestamp, *sender_agent_ids)
        with self._immediate_transaction():
            rows = self._connection.execute(
                f"""SELECT outbox_id, job_id, attempt_count FROM telegram_outbox
                   WHERE status = 'sending' AND lease_expires_at <= ?{agent_filter}
                   ORDER BY outbox_id""",
                parameters,
            ).fetchall()
            self._connection.execute(
                f"""UPDATE telegram_outbox
                   SET status = CASE WHEN attempt_count >= 20 THEN 'failed' ELSE 'pending' END,
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       error_code = 'stale_sender_lease', available_at = ?, updated_at = ?
                   WHERE status = 'sending' AND lease_expires_at <= ?{agent_filter}""",
                (timestamp, timestamp, timestamp, *parameters[1:]),
            )
            terminal_job_ids = [
                str(row["job_id"]) for row in rows if int(row["attempt_count"]) >= 20
            ]
            if terminal_job_ids:
                placeholders = ", ".join("?" for _ in terminal_job_ids)
                self._connection.execute(
                    f"""UPDATE provider_jobs
                        SET status = 'failed', error_class = 'telegram_delivery',
                            error_code = 'stale_sender_lease', error_detail = NULL,
                            updated_at = ?
                        WHERE status = 'result_ready' AND job_id IN ({placeholders})""",
                    (timestamp, *terminal_job_ids),
                )
        return tuple(str(row["outbox_id"]) for row in rows)

    def stage_handoff(
        self,
        topic_id: int,
        *,
        target_agent_id: str,
        source_agent_id: str,
        text: str,
    ) -> HandoffRecord:
        self.get_topic(topic_id)
        bounded = text.strip()[:20000]
        if not target_agent_id or not source_agent_id or not bounded:
            raise StateError("invalid handoff")
        handoff_id = str(uuid.uuid4())
        with self._connection:
            self._connection.execute(
                """INSERT INTO pending_handoffs
                   (handoff_id, topic_id, target_agent_id, source_agent_id, text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(topic_id, target_agent_id) DO UPDATE SET
                     handoff_id = excluded.handoff_id,
                     source_agent_id = excluded.source_agent_id,
                     text = excluded.text,
                     created_at = excluded.created_at""",
                (
                    handoff_id,
                    topic_id,
                    target_agent_id,
                    source_agent_id,
                    bounded,
                    _now(),
                ),
            )
        return HandoffRecord(handoff_id, topic_id, target_agent_id, source_agent_id, bounded)

    def recent_external_context(
        self, topic_id: int, agent_id: str, *, limit: int = 8
    ) -> str | None:
        if limit <= 0 or limit > 20:
            raise StateError("invalid external context limit")
        rows = self._connection.execute(
            """SELECT user_excerpt, response_excerpt, model, provider
               FROM external_turn_excerpts
               WHERE topic_id = ? AND agent_id = ?
               ORDER BY turn_id DESC LIMIT ?""",
            (topic_id, agent_id, limit),
        ).fetchall()
        if not rows:
            return None
        parts: list[str] = []
        for row in reversed(rows):
            label = (
                "/".join(value for value in (row["provider"], row["model"]) if value) or agent_id
            )
            parts.append(f"USER: {row['user_excerpt']}\n{label.upper()}: {row['response_excerpt']}")
        return "\n\n".join(parts)

    def record_visible_turn(
        self,
        topic_id: int,
        *,
        agent_id: str,
        provider: str,
        model: str,
        user_excerpt: str,
        response_excerpt: str,
        provider_session_id: str | None = None,
    ) -> int:
        self.get_topic(topic_id)
        if not agent_id or not user_excerpt.strip() or not response_excerpt.strip():
            raise StateError("invalid visible turn")
        with self._connection:
            cursor = self._connection.execute(
                """INSERT INTO external_turn_excerpts
                   (topic_id, agent_id, provider_session_id, model, provider,
                    user_excerpt, response_excerpt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    topic_id,
                    agent_id,
                    (provider_session_id or "")[:200],
                    model[:200],
                    provider[:200],
                    user_excerpt.strip()[:2000],
                    response_excerpt.strip()[:4000],
                    _now(),
                ),
            )
            if cursor.lastrowid is None:
                raise StateError("failed to persist visible turn")
            turn_id = cursor.lastrowid
            self._connection.execute(
                """DELETE FROM external_turn_excerpts
                   WHERE topic_id = ? AND turn_id NOT IN (
                     SELECT turn_id FROM external_turn_excerpts
                     WHERE topic_id = ? ORDER BY turn_id DESC LIMIT 100
                   )""",
                (topic_id, topic_id),
            )
        return turn_id

    def unseen_visible_context(
        self, topic_id: int, observer_agent_id: str, *, limit: int = 8
    ) -> tuple[str | None, int | None]:
        if not observer_agent_id or limit <= 0 or limit > 20:
            raise StateError("invalid visible context request")
        cursor = self._connection.execute(
            """SELECT last_turn_id FROM visible_context_cursors
               WHERE topic_id = ? AND observer_agent_id = ?""",
            (topic_id, observer_agent_id),
        ).fetchone()
        last_turn_id = int(cursor["last_turn_id"]) if cursor is not None else 0
        rows = self._connection.execute(
            """SELECT turn_id, agent_id, user_excerpt, response_excerpt, model, provider
               FROM external_turn_excerpts
               WHERE topic_id = ? AND agent_id != ? AND turn_id > ?
               ORDER BY turn_id DESC LIMIT ?""",
            (topic_id, observer_agent_id, last_turn_id, limit),
        ).fetchall()
        if not rows:
            return None, None
        parts: list[str] = []
        for row in reversed(rows):
            label = "/".join(
                value for value in (row["agent_id"], row["provider"], row["model"]) if value
            )
            parts.append(
                f"USER → {row['agent_id']}: {row['user_excerpt']}\n"
                f"{label.upper()}: {row['response_excerpt']}"
            )
        return "\n\n".join(parts), max(int(row["turn_id"]) for row in rows)

    def acknowledge_visible_context(
        self, topic_id: int, observer_agent_id: str, last_turn_id: int
    ) -> None:
        if not observer_agent_id or last_turn_id <= 0:
            raise StateError("invalid visible context cursor")
        with self._connection:
            self._connection.execute(
                """INSERT INTO visible_context_cursors
                   (topic_id, observer_agent_id, last_turn_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(topic_id, observer_agent_id) DO UPDATE SET
                     last_turn_id = MAX(last_turn_id, excluded.last_turn_id),
                     updated_at = excluded.updated_at""",
                (topic_id, observer_agent_id, last_turn_id, _now()),
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
        now = _now()
        self._connection.execute(
            """INSERT INTO agent_sessions
               (session_id, topic_id, agent_id, generation, status, model, effort,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, topic_id, agent_id, generation, status, model, effort, now, now),
        )
        return self.get_session(session_id)

    def activate_agent(
        self, topic_id: int, agent_id: str, model: str, effort: str
    ) -> SessionRecord:
        self.get_topic(topic_id)
        now = _now()
        with self._connection:
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
        topic = self.get_topic(topic_id)
        if topic.active_agent_id == agent_id:
            row = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
                (topic_id,),
            ).fetchone()
            if row is None:
                raise StateError("topic has active agent but no active session")
            return self._session(row)
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND agent_id = ? AND status = 'satellite'",
            (topic_id, agent_id),
        ).fetchone()
        if row is not None:
            return self._session(row)
        with self._connection:
            session = self._insert_session(topic_id, agent_id, model, effort, "satellite")
        return self.get_session(session.session_id)

    def new_active_session(self, topic_id: int) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
            (topic_id,),
        ).fetchone()
        if row is None:
            raise StateError("topic has no active session")
        previous = self._session(row)
        with self._connection:
            self._connection.execute(
                "UPDATE agent_sessions SET status = 'archived', updated_at = ? WHERE session_id = ?",
                (_now(), previous.session_id),
            )
            replacement = self._insert_session(
                topic_id, previous.agent_id, previous.model, previous.effort, "active"
            )
        return self.get_session(replacement.session_id)

    def replace_active_session(self, topic_id: int, *, model: str, effort: str) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
            (topic_id,),
        ).fetchone()
        if row is None:
            raise StateError("topic has no active session")
        previous = self._session(row)
        with self._connection:
            self._connection.execute(
                "UPDATE agent_sessions SET status = 'archived', updated_at = ? "
                "WHERE session_id = ?",
                (_now(), previous.session_id),
            )
            replacement = self._insert_session(topic_id, previous.agent_id, model, effort, "active")
        return self.get_session(replacement.session_id)

    def claim_message(self, chat_id: int, message_id: int, *, observer_agent_id: str) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO observed_messages VALUES (?, ?, ?, ?)",
                    (chat_id, message_id, observer_agent_id, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_bot_offset(self, agent_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT next_update_id FROM bot_offsets WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return None if row is None else int(row["next_update_id"])

    def set_bot_offset(self, agent_id: str, next_update_id: int) -> None:
        if next_update_id < 0:
            raise StateError("next update id cannot be negative")
        with self._connection:
            self._connection.execute(
                "INSERT INTO bot_offsets(agent_id, next_update_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET next_update_id = MAX("
                "bot_offsets.next_update_id, excluded.next_update_id), "
                "updated_at = excluded.updated_at",
                (agent_id, next_update_id, _now()),
            )

    def claim_callback(self, callback_id: str, *, observer_agent_id: str) -> bool:
        if not callback_id:
            raise StateError("callback id is empty")
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO observed_callbacks VALUES (?, ?, ?)",
                    (callback_id, observer_agent_id, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def status_snapshot(self) -> dict[str, object]:
        topics = self._connection.execute(
            """SELECT t.topic_id, t.project_id, t.chat_id, t.thread_id, t.title,
                      t.active_agent_id, s.session_id, s.provider_session_id,
                      s.writer_mode, s.model, s.effort
               FROM topics t
               LEFT JOIN agent_sessions s
                 ON s.topic_id = t.topic_id AND s.status = 'active'
               ORDER BY t.project_id, t.thread_id"""
        ).fetchall()
        offsets = self._connection.execute(
            "SELECT agent_id, next_update_id, updated_at FROM bot_offsets ORDER BY agent_id"
        ).fetchall()
        dispatch_counts = self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM turn_dispatches GROUP BY status"
        ).fetchall()
        running = self._connection.execute(
            """SELECT dispatch_id, topic_id, agent_id, status, created_at, updated_at
               FROM turn_dispatches WHERE status IN ('queued', 'running')
               ORDER BY created_at"""
        ).fetchall()
        runtime_events = self._connection.execute(
            """SELECT component, level, code, detail, created_at
               FROM runtime_events ORDER BY event_id DESC LIMIT 50"""
        ).fetchall()
        return {
            "schema_version": self.schema_version,
            "topics": [dict(row) for row in topics],
            "bot_offsets": [dict(row) for row in offsets],
            "dispatch_counts": {row["status"]: row["count"] for row in dispatch_counts},
            "pending_dispatches": [dict(row) for row in running],
            "runtime_events": [dict(row) for row in runtime_events],
        }

    def start_dispatch(
        self,
        *,
        chat_id: int,
        message_id: int,
        topic_id: int,
        agent_id: str,
    ) -> str:
        dispatch_id = str(uuid.uuid4())
        now = _now()
        with self._connection:
            self._connection.execute(
                """INSERT INTO turn_dispatches
                   (dispatch_id, chat_id, message_id, topic_id, agent_id, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'running', ?, ?)""",
                (dispatch_id, chat_id, message_id, topic_id, agent_id, now, now),
            )
        return dispatch_id

    def finish_dispatch(
        self, dispatch_id: str, *, success: bool, error_code: str | None = None
    ) -> None:
        status = "completed" if success else "failed"
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE turn_dispatches
                   SET status = ?, error_code = ?, updated_at = ?
                   WHERE dispatch_id = ?""",
                (status, error_code[:128] if error_code else None, _now(), dispatch_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"unknown dispatch_id: {dispatch_id}")

    def record_runtime_event(self, component: str, level: str, code: str, detail: str) -> None:
        if level not in {"info", "warning", "error"}:
            raise StateError("invalid runtime event level")
        with self._connection:
            self._connection.execute(
                """INSERT INTO runtime_events(component, level, code, detail, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (component[:64], level, code[:64], detail[:1000], _now()),
            )

    def latest_runtime_event(self, component: str, code: str) -> dict[str, object] | None:
        row = self._connection.execute(
            """SELECT component, level, code, detail, created_at FROM runtime_events
               WHERE component = ? AND code = ? ORDER BY event_id DESC LIMIT 1""",
            (component, code),
        ).fetchone()
        return dict(row) if row is not None else None

    def observe_runtime_counter(self, key: str, value: int) -> int | None:
        if not key.strip() or value < 0:
            raise StateError("invalid runtime counter")
        row = self._connection.execute(
            "SELECT integer_value FROM runtime_checkpoints WHERE checkpoint_key = ?",
            (key,),
        ).fetchone()
        previous = int(row["integer_value"]) if row is not None else None
        with self._connection:
            self._connection.execute(
                """INSERT INTO runtime_checkpoints
                   (checkpoint_key, integer_value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(checkpoint_key) DO UPDATE SET
                     integer_value = MAX(integer_value, excluded.integer_value),
                     updated_at = excluded.updated_at""",
                (key[:128], value, _now()),
            )
        return previous

    def runtime_counter(self, key: str) -> int | None:
        row = self._connection.execute(
            "SELECT integer_value FROM runtime_checkpoints WHERE checkpoint_key = ?",
            (key,),
        ).fetchone()
        return int(row["integer_value"]) if row is not None else None

    def set_runtime_counter(self, key: str, value: int) -> None:
        self.observe_runtime_counter(key, value)

    def active_topics_for_agent(self, agent_id: str) -> tuple[TopicRecord, ...]:
        rows = self._connection.execute(
            """SELECT t.* FROM topics t
               JOIN agent_sessions s ON s.topic_id = t.topic_id
               WHERE s.status = 'active' AND s.agent_id = ?
               ORDER BY t.topic_id""",
            (agent_id,),
        ).fetchall()
        return tuple(self._topic(row) for row in rows)

    def claim_alert_delivery(self, alert_key: str, *, cooldown_seconds: int) -> bool:
        if cooldown_seconds < 0:
            raise StateError("invalid alert delivery claim")
        key = _bounded(alert_key, name="alert delivery key", maximum=256)
        now = datetime.now(timezone.utc)
        with self._immediate_transaction():
            existing = self._connection.execute(
                "SELECT last_sent_at FROM alert_deliveries WHERE alert_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                try:
                    last_sent = datetime.fromisoformat(str(existing["last_sent_at"]))
                except ValueError:
                    last_sent = datetime.min.replace(tzinfo=timezone.utc)
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                if (now - last_sent).total_seconds() < cooldown_seconds:
                    return False
            self._connection.execute(
                """INSERT INTO alert_deliveries(alert_key, last_sent_at) VALUES (?, ?)
                   ON CONFLICT(alert_key) DO UPDATE SET last_sent_at = excluded.last_sent_at""",
                (key, now.isoformat()),
            )
        return True

    def release_alert_delivery(self, alert_key: str) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM alert_deliveries WHERE alert_key = ?",
                (alert_key[:256],),
            )

    def register_lane(
        self,
        *,
        lane_id: str,
        project_id: str,
        worktree_path: Path,
        branch_name: str,
        topic_id: int | None = None,
    ) -> None:
        now = _now()
        with self._connection:
            self._connection.execute(
                """INSERT INTO worktree_lanes
                   (lane_id, project_id, topic_id, worktree_path, branch_name,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    lane_id,
                    project_id,
                    topic_id,
                    str(worktree_path.resolve(strict=True)),
                    branch_name,
                    now,
                    now,
                ),
            )

    def archive_lane(self, lane_id: str) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE worktree_lanes SET status = 'archived', updated_at = ? WHERE lane_id = ?",
                (_now(), lane_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"unknown lane_id: {lane_id}")

    def mark_lane_cleaned(self, lane_id: str) -> None:
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE worktree_lanes SET cleaned_at = ?, updated_at = ?
                   WHERE lane_id = ? AND status = 'archived' AND cleaned_at IS NULL""",
                (_now(), _now(), lane_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"lane is unknown, active, or already cleaned: {lane_id}")

    def bind_lane(self, lane_id: str, topic_id: int) -> dict[str, object]:
        lane = self._connection.execute(
            "SELECT * FROM worktree_lanes WHERE lane_id = ?", (lane_id,)
        ).fetchone()
        if lane is None or lane["status"] != "active":
            raise StateError(f"unknown or inactive lane_id: {lane_id}")
        topic = self._connection.execute(
            "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if topic is None:
            raise StateError(f"unknown topic_id: {topic_id}")
        if lane["project_id"] != topic["project_id"]:
            raise StateError("lane and Telegram topic belong to different projects")
        conflict = self._connection.execute(
            """SELECT lane_id FROM worktree_lanes
               WHERE topic_id = ? AND lane_id != ? AND status = 'active'""",
            (topic_id, lane_id),
        ).fetchone()
        if conflict is not None:
            raise StateError("Telegram topic is already bound to another active lane")
        with self._connection:
            self._connection.execute(
                "UPDATE worktree_lanes SET topic_id = ?, updated_at = ? WHERE lane_id = ?",
                (topic_id, _now(), lane_id),
            )
        bound = self._connection.execute(
            "SELECT * FROM worktree_lanes WHERE lane_id = ?", (lane_id,)
        ).fetchone()
        if bound is None:
            raise StateError(f"unknown lane_id: {lane_id}")
        return dict(bound)

    def get_lane(self, lane_id: str) -> dict[str, object]:
        row = self._connection.execute(
            "SELECT * FROM worktree_lanes WHERE lane_id = ?", (lane_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown lane_id: {lane_id}")
        return dict(row)

    def list_lanes(self) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM worktree_lanes ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]
