"""Durable delivery-only task notices on the caller-owned SQLite connection."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

MAX_NOTICE_ATTEMPTS = 5
TransactionFactory = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True, slots=True)
class TaskLifecycleNotice:
    notice_id: str
    event_key: str
    kind: str
    job_id: str | None
    stop_request_id: str | None
    chat_id: int
    thread_id: int
    reply_to_message_id: int | None
    telegram_html: str
    status: str
    attempt_count: int
    available_at: str
    lease_token: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    send_started_at: str | None
    telegram_message_id: int | None
    error_code: str | None
    created_at: str
    updated_at: str


class TaskLifecycleState:
    """Own queries, never the connection, schema, or transaction commit policy."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: TransactionFactory,
        state_error: Callable[[str], Exception],
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.state_error = state_error

    def _time(self, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise self.state_error("notice timestamp must be timezone-aware")
        return now.astimezone(timezone.utc).isoformat()

    def _text(self, value: str, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise self.state_error(f"invalid task notice {name}")
        return value

    def _integer(self, value: int, name: str, *, positive: bool = True) -> None:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or (value <= 0 if positive else value == 0)
        ):
            raise self.state_error(f"invalid task notice {name}")

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskLifecycleNotice:
        return TaskLifecycleNotice(**dict(row))

    def get_notice(self, notice_id: str) -> TaskLifecycleNotice:
        row = self.db.execute(
            "SELECT * FROM task_lifecycle_notices WHERE notice_id=?", (notice_id,)
        ).fetchone()
        if row is None:
            raise self.state_error("task notice does not exist")
        return self._record(row)

    def notices_for_stop(self, stop_request_id: str) -> tuple[TaskLifecycleNotice, ...]:
        return tuple(
            self._record(row)
            for row in self.db.execute(
                "SELECT * FROM task_lifecycle_notices WHERE stop_request_id=? "
                "OR notice_id IN (SELECT notice_id FROM task_lifecycle_legacy_stop_links "
                "WHERE stop_request_id=?) ORDER BY created_at,notice_id",
                (stop_request_id, stop_request_id),
            ).fetchall()
        )

    def _validate_destination(
        self, job_id: str | None, stop_request_id: str | None, chat_id: int, thread_id: int
    ) -> None:
        if job_id is None and stop_request_id is None:
            raise self.state_error("task notice requires a job or stop reference")
        for identifier, table, column in (
            (job_id, "provider_jobs", "job_id"),
            (stop_request_id, "provider_stop_requests", "request_id"),
        ):
            if identifier is None:
                continue
            self._text(identifier, "subject reference", 128)
            row = self.db.execute(
                f"SELECT subject.chat_id,topics.chat_id AS topic_chat,topics.thread_id "
                f"FROM {table} subject JOIN topics ON topics.topic_id=subject.topic_id "
                f"WHERE subject.{column}=?",
                (identifier,),
            ).fetchone()
            if row is None or (
                int(row["chat_id"]) != chat_id
                or int(row["topic_chat"]) != chat_id
                or int(row["thread_id"]) != thread_id
            ):
                raise self.state_error("task notice destination does not match its subject")

    def prepare_notice_in_transaction(
        self,
        *,
        event_key: str,
        kind: str,
        job_id: str | None = None,
        stop_request_id: str | None = None,
        chat_id: int,
        thread_id: int,
        reply_to_message_id: int | None = None,
        telegram_html: str,
        now: datetime,
    ) -> tuple[TaskLifecycleNotice, bool]:
        if not self.db.in_transaction:
            raise self.state_error("task notice preparation requires an active transaction")
        self._text(event_key, "event key", 256)
        self._text(kind, "kind", 64)
        self._text(telegram_html, "HTML", 3500)
        self._integer(chat_id, "chat identity", positive=False)
        self._integer(thread_id, "thread identity")
        if reply_to_message_id is not None:
            self._integer(reply_to_message_id, "reply identity")
        timestamp = self._time(now)
        self._validate_destination(job_id, stop_request_id, chat_id, thread_id)
        immutable = (
            kind,
            job_id,
            stop_request_id,
            chat_id,
            thread_id,
            reply_to_message_id,
            telegram_html,
        )
        prior = self.db.execute(
            "SELECT * FROM task_lifecycle_notices WHERE event_key=?", (event_key,)
        ).fetchone()
        if prior is not None:
            fields = (
                "kind",
                "job_id",
                "stop_request_id",
                "chat_id",
                "thread_id",
                "reply_to_message_id",
                "telegram_html",
            )
            if tuple(prior[field] for field in fields) != immutable:
                raise self.state_error("task notice event content is immutable")
            return self._record(prior), False
        notice_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO task_lifecycle_notices "
            "(notice_id,event_key,kind,job_id,stop_request_id,chat_id,thread_id,"
            "reply_to_message_id,telegram_html,status,available_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (notice_id, event_key, *immutable, timestamp, timestamp, timestamp),
        )
        return self.get_notice(notice_id), True

    def lease_notice(
        self, sender_id: str, *, now: datetime, lease_seconds: int = 90
    ) -> TaskLifecycleNotice | None:
        self._text(sender_id, "sender identity", 128)
        self._integer(lease_seconds, "lease duration")
        if lease_seconds > 3600:
            raise self.state_error("invalid task notice lease duration")
        timestamp = self._time(now)
        expires = self._time(now + timedelta(seconds=lease_seconds))
        with self.transaction():
            row = self.db.execute(
                "SELECT notice_id FROM task_lifecycle_notices WHERE status='pending' "
                "AND available_at<=? ORDER BY created_at,notice_id LIMIT 1",
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE task_lifecycle_notices SET status='leased',lease_token=?,"
                "lease_owner=?,lease_expires_at=?,send_started_at=NULL,updated_at=? "
                "WHERE notice_id=? AND status='pending'",
                (uuid.uuid4().hex, sender_id, expires, timestamp, row["notice_id"]),
            )
            return self.get_notice(str(row["notice_id"]))

    def _attempt(
        self, notice_id: str, token: str, timestamp: str, *, started: bool = True
    ) -> TaskLifecycleNotice:
        notice = self.get_notice(notice_id)
        if (
            notice.status != "leased"
            or notice.lease_token != token
            or notice.lease_expires_at is None
            or notice.lease_expires_at <= timestamp
            or (notice.send_started_at is not None) != started
        ):
            raise self.state_error("task notice requires a current matching delivery lease")
        return notice

    def begin_send(self, notice_id: str, lease_token: str, *, now: datetime) -> TaskLifecycleNotice:
        timestamp = self._time(now)
        with self.transaction():
            self._attempt(notice_id, lease_token, timestamp, started=False)
            self.db.execute(
                "UPDATE task_lifecycle_notices SET send_started_at=?,"
                "attempt_count=attempt_count+1,updated_at=? WHERE notice_id=?",
                (timestamp, timestamp, notice_id),
            )
            return self.get_notice(notice_id)

    def complete_send(
        self, notice_id: str, lease_token: str, *, telegram_message_id: int, now: datetime
    ) -> TaskLifecycleNotice:
        self._integer(telegram_message_id, "Telegram receipt")
        timestamp = self._time(now)
        with self.transaction():
            self._attempt(notice_id, lease_token, timestamp)
            self.db.execute(
                "UPDATE task_lifecycle_notices SET status='delivered',telegram_message_id=?,"
                "lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,error_code=NULL,"
                "updated_at=? WHERE notice_id=?",
                (telegram_message_id, timestamp, notice_id),
            )
            return self.get_notice(notice_id)

    def retry_rejected(
        self,
        notice_id: str,
        lease_token: str,
        *,
        error_code: str,
        available_at: datetime,
        now: datetime,
        max_attempts: int = MAX_NOTICE_ATTEMPTS,
    ) -> TaskLifecycleNotice:
        self._text(error_code, "error code", 128)
        self._integer(max_attempts, "attempt limit")
        if max_attempts > 20:
            raise self.state_error("invalid task notice attempt limit")
        timestamp, due = self._time(now), self._time(available_at)
        if due < timestamp:
            raise self.state_error("task notice retry cannot precede rejection")
        with self.transaction():
            notice = self._attempt(notice_id, lease_token, timestamp)
            status = "failed" if notice.attempt_count >= max_attempts else "pending"
            self.db.execute(
                "UPDATE task_lifecycle_notices SET status=?,available_at=?,error_code=?,"
                "lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE notice_id=?",
                (status, due, error_code, timestamp, notice_id),
            )
            return self.get_notice(notice_id)

    def mark_send_unknown(
        self, notice_id: str, lease_token: str, *, error_code: str, now: datetime
    ) -> TaskLifecycleNotice:
        self._text(error_code, "error code", 128)
        timestamp = self._time(now)
        with self.transaction():
            notice = self.get_notice(notice_id)
            # A receipt committed before an exception remains affirmative evidence.
            if notice.status == "delivered":
                return notice
            if (
                notice.status != "leased"
                or notice.lease_token != lease_token
                or notice.send_started_at is None
            ):
                raise self.state_error("unknown delivery requires the matching begun attempt")
            self.db.execute(
                "UPDATE task_lifecycle_notices SET status='unknown',error_code=?,"
                "lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE notice_id=?",
                (error_code, timestamp, notice_id),
            )
            return self.get_notice(notice_id)

    def recover_expired_notices(self, *, now: datetime) -> int:
        timestamp = self._time(now)
        with self.transaction():
            changed = self.db.execute(
                "UPDATE task_lifecycle_notices SET status=CASE WHEN send_started_at IS NULL "
                "THEN 'pending' ELSE 'unknown' END,error_code=CASE WHEN send_started_at IS NULL "
                "THEN error_code ELSE 'expired_send_attempt' END,lease_token=NULL,"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE status='leased' AND lease_expires_at<=?",
                (timestamp, timestamp),
            )
            return changed.rowcount
