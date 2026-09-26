from __future__ import annotations

import html
import sqlite3
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Callable, Sequence

from .artifacts import ValidatedArtifact
from .telegram_multipart import split_telegram_html

MAX_PROGRESS_ATTEMPTS = 20
PROGRESS_INTERVAL_SECONDS = 120
PROGRESS_LEASE_SECONDS = 120
_MAX_PROGRESS_SOURCE_LENGTH = 600


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
class TelegramOutboxPartRecord:
    outbox_id: str
    part_index: int
    telegram_html: str
    part_type: str = "text"
    file_path: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    file_sha256: str | None = None
    telegram_message_id: int | None = None
    delivered_at: str | None = None


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


StateErrorFactory = Callable[[str], Exception]
TransactionFactory = Callable[[], AbstractContextManager[None]]


class DeliveryStateFacade:
    """Final and progress delivery state on the HubState-owned connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: TransactionFactory,
        write_transaction: TransactionFactory,
        state_error: StateErrorFactory,
    ) -> None:
        self._connection = connection
        self._transaction = transaction
        self._write_transaction = write_transaction
        self._state_error = state_error

    def _bounded(self, value: str, *, name: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise self._state_error(f"invalid {name}")
        return normalized

    def _timestamp(self, value: datetime | None = None) -> str:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise self._state_error("timestamp must be timezone-aware")
        return current.astimezone(timezone.utc).isoformat()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _utc_now(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise self._state_error("progress delivery time must include a timezone")
        return current.astimezone(timezone.utc)

    def _progress_html(self, text: str) -> str:
        bounded = text.strip()[:_MAX_PROGRESS_SOURCE_LENGTH]
        if not bounded:
            raise self._state_error("progress delivery requires visible text")
        rendered = escape(bounded)
        if len(rendered) > 4096:
            raise self._state_error("progress delivery exceeds Telegram limit")
        return rendered

    @staticmethod
    def outbox_record(row: sqlite3.Row) -> TelegramOutboxRecord:
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

    @staticmethod
    def outbox_part_record(row: sqlite3.Row) -> TelegramOutboxPartRecord:
        keys = row.keys() if hasattr(row, "keys") else ()
        return TelegramOutboxPartRecord(
            outbox_id=str(row["outbox_id"]),
            part_index=int(row["part_index"]),
            telegram_html=str(row["telegram_html"]),
            part_type=str(row["part_type"]) if "part_type" in keys else "text",
            file_path=(
                str(row["file_path"])
                if "file_path" in keys and row["file_path"] is not None
                else None
            ),
            file_name=(
                str(row["file_name"])
                if "file_name" in keys and row["file_name"] is not None
                else None
            ),
            file_size=(
                int(row["file_size"])
                if "file_size" in keys and row["file_size"] is not None
                else None
            ),
            file_sha256=(
                str(row["file_sha256"])
                if "file_sha256" in keys and row["file_sha256"] is not None
                else None
            ),
            telegram_message_id=row["telegram_message_id"],
            delivered_at=row["delivered_at"],
        )

    @staticmethod
    def progress_record(row: sqlite3.Row) -> ProgressDeliveryRecord:
        return ProgressDeliveryRecord(**dict(row))

    def insert_outbox_parts(
        self,
        outbox_id: str,
        telegram_html: str,
        artifacts: tuple[ValidatedArtifact, ...] = (),
    ) -> None:
        parts = split_telegram_html(telegram_html)
        for part_index, part in enumerate(parts, start=1):
            self._connection.execute(
                """INSERT INTO telegram_outbox_parts
                   (outbox_id, part_index, telegram_html, part_type, file_path, file_name,
                    file_size, file_sha256)
                   VALUES (?, ?, ?, 'text', NULL, NULL, NULL, NULL)""",
                (outbox_id, part_index, part),
            )
        start_index = len(parts) + 1
        for offset, artifact in enumerate(artifacts):
            idx = start_index + offset
            caption = f"📄 <b>{html.escape(artifact.name)}</b>"
            self._connection.execute(
                """INSERT INTO telegram_outbox_parts
                   (outbox_id, part_index, telegram_html, part_type, file_path, file_name,
                    file_size, file_sha256)
                   VALUES (?, ?, ?, 'document', ?, ?, ?, ?)""",
                (
                    outbox_id,
                    idx,
                    caption,
                    str(artifact.path),
                    artifact.name,
                    artifact.size,
                    artifact.sha256,
                ),
            )

    def get_outbox(self, outbox_id: str) -> TelegramOutboxRecord:
        row = self._connection.execute(
            "SELECT * FROM telegram_outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise self._state_error(f"unknown Telegram outbox row: {outbox_id}")
        return self.outbox_record(row)

    def get_outbox_for_job(self, job_id: str) -> TelegramOutboxRecord:
        row = self._connection.execute(
            "SELECT * FROM telegram_outbox WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise self._state_error(f"provider job has no Telegram outbox row: {job_id}")
        return self.outbox_record(row)

    def get_outbox_parts(self, outbox_id: str) -> tuple[TelegramOutboxPartRecord, ...]:
        rows = self._connection.execute(
            """SELECT * FROM telegram_outbox_parts
               WHERE outbox_id = ? ORDER BY part_index""",
            (outbox_id,),
        ).fetchall()
        return tuple(self.outbox_part_record(row) for row in rows)

    def next_outbox_part(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> TelegramOutboxPartRecord:
        timestamp = self._timestamp(now)
        row = self._connection.execute(
            """SELECT parts.* FROM telegram_outbox_parts parts
               JOIN telegram_outbox outbox ON outbox.outbox_id = parts.outbox_id
               WHERE parts.outbox_id = ? AND parts.telegram_message_id IS NULL
                 AND outbox.status = 'sending' AND outbox.lease_token = ?
                 AND outbox.lease_expires_at > ?
               ORDER BY parts.part_index LIMIT 1""",
            (outbox_id, lease_token, timestamp),
        ).fetchone()
        if row is None:
            raise self._state_error("Telegram outbox has no sendable part for this lease")
        return self.outbox_part_record(row)

    def lease_outbox(
        self,
        sender_agent_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord | None:
        sender = self._bounded(sender_agent_id, name="sender agent id", maximum=64)
        worker = self._bounded(worker_id, name="worker id", maximum=128)
        if not 1 <= lease_seconds <= 3600:
            raise self._state_error("invalid Telegram outbox lease duration")
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        expires_at = self._timestamp(current + timedelta(seconds=lease_seconds))
        with self._transaction():
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
                raise self._state_error("Telegram outbox lease race")
            leased = self._connection.execute(
                "SELECT * FROM telegram_outbox WHERE outbox_id = ?", (row["outbox_id"],)
            ).fetchone()
            if leased is None:
                raise self._state_error("leased Telegram outbox row disappeared")
            return self.outbox_record(leased)

    def heartbeat_outbox(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord:
        if not 1 <= lease_seconds <= 3600:
            raise self._state_error("invalid Telegram outbox lease duration")
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        expires_at = self._timestamp(current + timedelta(seconds=lease_seconds))
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE telegram_outbox SET lease_expires_at = ?, updated_at = ?
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (expires_at, timestamp, outbox_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise self._state_error("Telegram outbox lease is missing, expired, or invalid")
        return self.get_outbox(outbox_id)

    def release_outbox_lease(self, outbox_id: str, lease_token: str) -> None:
        timestamp = self._now()
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE telegram_outbox
                   SET status = 'pending', attempt_count = MAX(attempt_count - 1, 0),
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       error_code = NULL, available_at = ?, updated_at = ?
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?""",
                (timestamp, timestamp, outbox_id, lease_token),
            )
        if cursor.rowcount != 1:
            raise self._state_error("Telegram outbox lease is missing or invalid")

    def retry_outbox(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord:
        code = self._bounded(error_code, name="error code", maximum=128)
        if not 0 <= delay_seconds <= 86400:
            raise self._state_error("invalid Telegram outbox retry delay")
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        available_at = self._timestamp(current + timedelta(seconds=delay_seconds))
        with self._transaction():
            row = self._connection.execute(
                """SELECT job_id, attempt_count FROM telegram_outbox
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (outbox_id, lease_token, timestamp),
            ).fetchone()
            if row is None:
                raise self._state_error("Telegram outbox lease is missing, expired, or invalid")
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
                raise self._state_error("Telegram outbox lease is missing, expired, or invalid")
            if int(row["attempt_count"]) >= 20:
                self._connection.execute(
                    """UPDATE provider_jobs
                       SET status = 'failed', error_class = 'telegram_delivery',
                           error_code = ?, error_detail = NULL, updated_at = ?
                       WHERE job_id = ? AND status = 'result_ready'""",
                    (code, timestamp, row["job_id"]),
                )
        return self.get_outbox(outbox_id)

    def mark_outbox_delivered(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        telegram_message_id: int,
        now: datetime | None = None,
    ) -> TelegramOutboxRecord:
        if telegram_message_id <= 0:
            raise self._state_error("invalid Telegram message id")
        timestamp = self._timestamp(now)
        with self._transaction():
            row = self._connection.execute(
                """SELECT job_id, sender_agent_id FROM telegram_outbox
                   WHERE outbox_id = ? AND status = 'sending' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (outbox_id, lease_token, timestamp),
            ).fetchone()
            if row is None:
                raise self._state_error("Telegram outbox lease is missing or invalid")
            part = self._connection.execute(
                """SELECT part_index FROM telegram_outbox_parts
                   WHERE outbox_id = ? AND telegram_message_id IS NULL
                   ORDER BY part_index LIMIT 1""",
                (outbox_id,),
            ).fetchone()
            if part is None:
                raise self._state_error("Telegram outbox has no undelivered part")
            self._connection.execute(
                """UPDATE telegram_outbox_parts
                   SET telegram_message_id = ?, delivered_at = ?
                   WHERE outbox_id = ? AND part_index = ? AND telegram_message_id IS NULL""",
                (telegram_message_id, timestamp, outbox_id, part["part_index"]),
            )
            remaining = self._connection.execute(
                """SELECT 1 FROM telegram_outbox_parts
                   WHERE outbox_id = ? AND telegram_message_id IS NULL LIMIT 1""",
                (outbox_id,),
            ).fetchone()
            if remaining is not None:
                self._connection.execute(
                    """UPDATE telegram_outbox
                       SET status = 'pending', attempt_count = MAX(attempt_count - 1, 0),
                           available_at = ?, lease_owner = NULL,
                           lease_token = NULL, lease_expires_at = NULL, error_code = NULL,
                           updated_at = ? WHERE outbox_id = ? AND status = 'sending'
                             AND lease_token = ? AND lease_expires_at > ?""",
                    (timestamp, timestamp, outbox_id, lease_token, timestamp),
                )
            else:
                self._connection.execute(
                    """UPDATE telegram_outbox
                       SET status = 'delivered', telegram_message_id = ?, delivered_at = ?,
                           lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                           updated_at = ? WHERE outbox_id = ? AND status = 'sending'
                             AND lease_token = ? AND lease_expires_at > ?""",
                    (
                        telegram_message_id,
                        timestamp,
                        timestamp,
                        outbox_id,
                        lease_token,
                        timestamp,
                    ),
                )
                cursor = self._connection.execute(
                    """UPDATE provider_jobs SET status = 'completed', updated_at = ?
                       WHERE job_id = ? AND status = 'result_ready'""",
                    (timestamp, row["job_id"]),
                )
                if cursor.rowcount != 1:
                    terminal = self._connection.execute(
                        "SELECT status FROM provider_jobs WHERE job_id = ?",
                        (row["job_id"],),
                    ).fetchone()
                    allowed_terminal = {"failed", "indeterminate"}
                    if str(row["sender_agent_id"]) == "hub":
                        allowed_terminal.add("cancelled")
                    if terminal is None or str(terminal["status"]) not in allowed_terminal:
                        raise self._state_error("provider job is not ready for Telegram completion")
            delivered = self._connection.execute(
                "SELECT * FROM telegram_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if delivered is None:
                raise self._state_error("delivered Telegram outbox row disappeared")
            result = self.outbox_record(delivered)
        return result

    def recover_stale_outbox(
        self,
        *,
        sender_agent_ids: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        timestamp = self._timestamp(now)
        agent_filter = ""
        parameters: tuple[object, ...] = (timestamp,)
        if sender_agent_ids is not None:
            if not sender_agent_ids:
                return ()
            placeholders = ", ".join("?" for _ in sender_agent_ids)
            agent_filter = f" AND sender_agent_id IN ({placeholders})"
            parameters = (timestamp, *sender_agent_ids)
        with self._transaction():
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

    def get_progress(self, progress_id: str) -> ProgressDeliveryRecord:
        row = self._connection.execute(
            "SELECT * FROM provider_progress_deliveries WHERE progress_id = ?",
            (progress_id,),
        ).fetchone()
        if row is None:
            raise self._state_error("unknown progress delivery")
        return self.progress_record(row)

    def progress_for_job(self, job_id: str) -> tuple[ProgressDeliveryRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM provider_progress_deliveries WHERE job_id = ? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        return tuple(self.progress_record(row) for row in rows)

    def enqueue_progress(
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
            raise self._state_error("invalid progress delivery boundary")
        with self._transaction():
            return self.enqueue_progress_in_transaction(
                job_id,
                lease_token,
                item_sequence,
                text,
                now=now,
                min_interval_seconds=min_interval_seconds,
            )

    def enqueue_progress_in_transaction(
        self,
        job_id: str,
        lease_token: str,
        item_sequence: int,
        text: str,
        *,
        now: datetime | None = None,
        min_interval_seconds: int = PROGRESS_INTERVAL_SECONDS,
    ) -> bool:
        if not self._connection.in_transaction:
            raise self._state_error("progress enqueue requires an active transaction")
        if item_sequence <= 0 or min_interval_seconds < 0:
            raise self._state_error("invalid progress delivery boundary")
        current = self._utc_now(now)
        current_text = current.isoformat()
        rendered = self._progress_html(text)
        job = self._connection.execute(
            "SELECT jobs.agent_id, jobs.chat_id, topics.thread_id "
            "FROM provider_jobs jobs JOIN topics ON topics.topic_id = jobs.topic_id "
            "WHERE jobs.job_id = ? AND jobs.status = 'executing' "
            "AND jobs.lease_token = ? AND jobs.lease_expires_at > ?",
            (job_id, lease_token, current_text),
        ).fetchone()
        if job is None:
            raise self._state_error("progress delivery requires a current invocation lease")
        item = self._connection.execute(
            "SELECT phase FROM provider_visible_items WHERE sequence = ? AND job_id = ?",
            (item_sequence, job_id),
        ).fetchone()
        if item is None or item["phase"] != "commentary":
            raise self._state_error("progress delivery requires a commentary journal item")
        if self._connection.execute(
            "SELECT 1 FROM provider_progress_deliveries WHERE item_sequence = ?",
            (item_sequence,),
        ).fetchone():
            return False
        latest = self._connection.execute(
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
        self._connection.execute(
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

    def recover_stale_progress(
        self, sender_agent_ids: Sequence[str], *, now: datetime | None = None
    ) -> int:
        if not sender_agent_ids:
            return 0
        current = self._utc_now(now).isoformat()
        placeholders = ",".join("?" for _ in sender_agent_ids)
        with self._transaction():
            changed = self._connection.execute(
                f"UPDATE provider_progress_deliveries SET "
                "status = CASE WHEN attempt_count >= ? THEN 'failed' ELSE 'pending' END, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                f"WHERE sender_agent_id IN ({placeholders}) AND status = 'sending' "
                "AND lease_expires_at <= ?",
                (MAX_PROGRESS_ATTEMPTS, current, *sender_agent_ids, current),
            )
            return changed.rowcount

    def supersede_terminal_progress(
        self, sender_agent_ids: Sequence[str], *, now: datetime | None = None
    ) -> int:
        if not sender_agent_ids:
            return 0
        current = self._utc_now(now).isoformat()
        placeholders = ",".join("?" for _ in sender_agent_ids)
        with self._transaction():
            changed = self._connection.execute(
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

    def lease_progress(
        self,
        sender_agent_id: str,
        lease_owner: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = PROGRESS_LEASE_SECONDS,
    ) -> ProgressDeliveryRecord | None:
        if lease_seconds <= 0:
            raise self._state_error("progress lease must be positive")
        current_dt = self._utc_now(now)
        current = current_dt.isoformat()
        with self._transaction():
            row = self._connection.execute(
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
            self._connection.execute(
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
            return self.get_progress(str(row[0]))

    def release_progress(
        self, progress_id: str, lease_token: str, *, now: datetime | None = None
    ) -> None:
        current = self._utc_now(now).isoformat()
        with self._transaction():
            changed = self._connection.execute(
                "UPDATE provider_progress_deliveries SET status = 'pending', "
                "attempt_count = attempt_count - 1, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE progress_id = ? AND status = 'sending' AND lease_token = ?",
                (current, progress_id, lease_token),
            )
            if changed.rowcount != 1:
                raise self._state_error("progress delivery lease changed")

    def retry_progress(
        self,
        progress_id: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: float,
        now: datetime | None = None,
    ) -> None:
        if delay_seconds < 0:
            raise self._state_error("progress retry delay cannot be negative")
        current_dt = self._utc_now(now)
        current = current_dt.isoformat()
        with self._transaction():
            row = self._connection.execute(
                "SELECT attempt_count FROM provider_progress_deliveries "
                "WHERE progress_id = ? AND status = 'sending' AND lease_token = ?",
                (progress_id, lease_token),
            ).fetchone()
            if row is None:
                raise self._state_error("progress delivery lease changed")
            terminal = int(row[0]) >= MAX_PROGRESS_ATTEMPTS
            self._connection.execute(
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

    def mark_progress_delivered(
        self,
        progress_id: str,
        lease_token: str,
        *,
        telegram_message_id: int,
        now: datetime | None = None,
    ) -> None:
        if telegram_message_id <= 0:
            raise self._state_error("progress Telegram message id must be positive")
        current = self._utc_now(now).isoformat()
        with self._transaction():
            changed = self._connection.execute(
                "UPDATE provider_progress_deliveries SET status = 'delivered', "
                "telegram_message_id = ?, delivered_at = ?, updated_at = ?, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, error_code = NULL "
                "WHERE progress_id = ? AND status = 'sending' AND lease_token = ?",
                (telegram_message_id, current, current, progress_id, lease_token),
            )
            if changed.rowcount != 1:
                raise self._state_error("progress delivery lease changed")
