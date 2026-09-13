from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Sequence

from .state import HubState, StateError

MAX_PROGRESS_ATTEMPTS = 20
PROGRESS_INTERVAL_SECONDS = 120
PROGRESS_LEASE_SECONDS = 120
_MAX_PROGRESS_SOURCE_LENGTH = 600


@dataclass(frozen=True, slots=True)
class ProgressDeliveryRecord:
    progress_id: str
    item_sequence: int
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


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise StateError("progress delivery time must include a timezone")
    return current.astimezone(timezone.utc)


def _progress_html(text: str) -> str:
    bounded = text.strip()[:_MAX_PROGRESS_SOURCE_LENGTH]
    if not bounded:
        raise StateError("progress delivery requires visible text")
    rendered = f"<i>Progress</i>\n{escape(bounded)}"
    if len(rendered) > 4096:
        raise StateError("progress delivery exceeds Telegram limit")
    return rendered


class ProgressDeliveryQueue:
    """Durable advisory delivery isolated from provider job completion."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ProgressDeliveryRecord:
        return ProgressDeliveryRecord(**dict(row))

    def get(self, progress_id: str) -> ProgressDeliveryRecord:
        row = self.connection.execute(
            "SELECT * FROM provider_progress_deliveries WHERE progress_id = ?",
            (progress_id,),
        ).fetchone()
        if row is None:
            raise StateError("unknown progress delivery")
        return self._record(row)

    def for_job(self, job_id: str) -> tuple[ProgressDeliveryRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM provider_progress_deliveries WHERE job_id = ? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def enqueue(
        self,
        job_id: str,
        lease_token: str,
        item_sequence: int,
        text: str,
        *,
        now: datetime | None = None,
        min_interval_seconds: int = PROGRESS_INTERVAL_SECONDS,
    ) -> bool:
        if item_sequence <= 0 or min_interval_seconds < 0:
            raise StateError("invalid progress delivery boundary")
        with self.state._immediate_transaction():
            return self.enqueue_in_transaction(
                job_id,
                lease_token,
                item_sequence,
                text,
                now=now,
                min_interval_seconds=min_interval_seconds,
            )

    def enqueue_in_transaction(
        self,
        job_id: str,
        lease_token: str,
        item_sequence: int,
        text: str,
        *,
        now: datetime | None = None,
        min_interval_seconds: int = PROGRESS_INTERVAL_SECONDS,
    ) -> bool:
        """Queue progress atomically with its journal item."""
        if not self.connection.in_transaction:
            raise StateError("progress enqueue requires an active transaction")
        if item_sequence <= 0 or min_interval_seconds < 0:
            raise StateError("invalid progress delivery boundary")
        current = _utc_now(now)
        current_text = current.isoformat()
        rendered = _progress_html(text)
        job = self.connection.execute(
            "SELECT jobs.agent_id, jobs.chat_id, topics.thread_id "
            "FROM provider_jobs jobs JOIN topics ON topics.topic_id = jobs.topic_id "
            "WHERE jobs.job_id = ? AND jobs.status = 'executing' "
            "AND jobs.lease_token = ? AND jobs.lease_expires_at > ?",
            (job_id, lease_token, current_text),
        ).fetchone()
        if job is None:
            raise StateError("progress delivery requires a current invocation lease")
        item = self.connection.execute(
            "SELECT phase FROM provider_visible_items WHERE sequence = ? AND job_id = ?",
            (item_sequence, job_id),
        ).fetchone()
        if item is None or item["phase"] != "commentary":
            raise StateError("progress delivery requires a commentary journal item")
        if self.connection.execute(
            "SELECT 1 FROM provider_progress_deliveries WHERE item_sequence = ?",
            (item_sequence,),
        ).fetchone():
            return False
        latest = self.connection.execute(
            "SELECT created_at FROM provider_progress_deliveries "
            "WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if latest is not None:
            earliest = datetime.fromisoformat(str(latest[0])) + timedelta(
                seconds=min_interval_seconds
            )
            if current < earliest:
                return False
        self.connection.execute(
            "INSERT INTO provider_progress_deliveries "
            "(progress_id, item_sequence, job_id, sender_agent_id, chat_id, thread_id, "
            "telegram_html, status, available_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                str(uuid.uuid4()),
                item_sequence,
                job_id,
                str(job["agent_id"]),
                int(job["chat_id"]),
                int(job["thread_id"]),
                rendered,
                current_text,
                current_text,
                current_text,
            ),
        )
        return True

    def recover_stale(self, sender_agent_ids: Sequence[str], *, now: datetime | None = None) -> int:
        if not sender_agent_ids:
            return 0
        current = _utc_now(now).isoformat()
        placeholders = ",".join("?" for _ in sender_agent_ids)
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                f"UPDATE provider_progress_deliveries SET "
                "status = CASE WHEN attempt_count >= ? THEN 'failed' ELSE 'pending' END, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                f"WHERE sender_agent_id IN ({placeholders}) AND status = 'sending' "
                "AND lease_expires_at <= ?",
                (MAX_PROGRESS_ATTEMPTS, current, *sender_agent_ids, current),
            )
            return changed.rowcount

    def supersede_terminal(
        self, sender_agent_ids: Sequence[str], *, now: datetime | None = None
    ) -> int:
        if not sender_agent_ids:
            return 0
        current = _utc_now(now).isoformat()
        placeholders = ",".join("?" for _ in sender_agent_ids)
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                "UPDATE provider_progress_deliveries SET status = 'superseded', updated_at = ?, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
                "WHERE status = 'pending' "
                f"AND sender_agent_id IN ({placeholders}) "
                "AND EXISTS (SELECT 1 FROM provider_jobs jobs "
                "WHERE jobs.job_id = provider_progress_deliveries.job_id "
                "AND jobs.status != 'executing')",
                (current, *sender_agent_ids),
            )
            return changed.rowcount

    def lease(
        self,
        sender_agent_id: str,
        lease_owner: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = PROGRESS_LEASE_SECONDS,
    ) -> ProgressDeliveryRecord | None:
        if lease_seconds <= 0:
            raise StateError("progress lease must be positive")
        current_dt = _utc_now(now)
        current = current_dt.isoformat()
        with self.state._immediate_transaction():
            row = self.connection.execute(
                "SELECT progress.progress_id FROM provider_progress_deliveries progress "
                "JOIN provider_jobs jobs ON jobs.job_id = progress.job_id "
                "WHERE progress.sender_agent_id = ? AND progress.status = 'pending' "
                "AND progress.available_at <= ? AND progress.attempt_count < ? "
                "AND jobs.status = 'executing' ORDER BY progress.created_at LIMIT 1",
                (sender_agent_id, current, MAX_PROGRESS_ATTEMPTS),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            self.connection.execute(
                "UPDATE provider_progress_deliveries SET status = 'sending', "
                "attempt_count = attempt_count + 1, lease_owner = ?, lease_token = ?, "
                "lease_expires_at = ?, updated_at = ? WHERE progress_id = ?",
                (
                    lease_owner,
                    token,
                    (current_dt + timedelta(seconds=lease_seconds)).isoformat(),
                    current,
                    str(row[0]),
                ),
            )
            return self.get(str(row[0]))

    def release(self, progress_id: str, lease_token: str, *, now: datetime | None = None) -> None:
        current = _utc_now(now).isoformat()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                "UPDATE provider_progress_deliveries SET status = 'pending', "
                "attempt_count = attempt_count - 1, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE progress_id = ? AND status = 'sending' AND lease_token = ?",
                (current, progress_id, lease_token),
            )
            if changed.rowcount != 1:
                raise StateError("progress delivery lease changed")

    def retry(
        self,
        progress_id: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: float,
        now: datetime | None = None,
    ) -> None:
        if delay_seconds < 0:
            raise StateError("progress retry delay cannot be negative")
        current_dt = _utc_now(now)
        current = current_dt.isoformat()
        with self.state._immediate_transaction():
            row = self.connection.execute(
                "SELECT attempt_count FROM provider_progress_deliveries "
                "WHERE progress_id = ? AND status = 'sending' AND lease_token = ?",
                (progress_id, lease_token),
            ).fetchone()
            if row is None:
                raise StateError("progress delivery lease changed")
            terminal = int(row[0]) >= MAX_PROGRESS_ATTEMPTS
            self.connection.execute(
                "UPDATE provider_progress_deliveries SET status = ?, available_at = ?, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
                "error_code = ?, updated_at = ? WHERE progress_id = ?",
                (
                    "failed" if terminal else "pending",
                    (current_dt + timedelta(seconds=delay_seconds)).isoformat(),
                    error_code[:128],
                    current,
                    progress_id,
                ),
            )

    def mark_delivered(
        self,
        progress_id: str,
        lease_token: str,
        *,
        telegram_message_id: int,
        now: datetime | None = None,
    ) -> None:
        if telegram_message_id <= 0:
            raise StateError("progress Telegram message id must be positive")
        current = _utc_now(now).isoformat()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                "UPDATE provider_progress_deliveries SET status = 'delivered', "
                "telegram_message_id = ?, delivered_at = ?, updated_at = ?, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, error_code = NULL "
                "WHERE progress_id = ? AND status = 'sending' AND lease_token = ?",
                (telegram_message_id, current, current, progress_id, lease_token),
            )
            if changed.rowcount != 1:
                raise StateError("progress delivery lease changed")
