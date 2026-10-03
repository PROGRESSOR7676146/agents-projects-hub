"""Atomic topic stop intent and independent control delivery."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Callable

from .state_provider_jobs import ProviderJobsStateFacade
from .task_lifecycle import TaskLifecycleState


class StopState:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
        jobs: ProviderJobsStateFacade,
        notices: TaskLifecycleState,
        state_error: Callable[[str], Exception],
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.jobs = jobs
        self.notices = notices
        self.error = state_error

    def request(
        self,
        *,
        topic_id: int,
        chat_id: int,
        message_id: int,
        target_agent_id: str,
        prepare_notice: bool = False,
    ) -> tuple[str, int, bool]:
        if not target_agent_id.strip() or len(target_agent_id) > 64:
            raise self.error("invalid agent id")
        with self.transaction():
            now = datetime.now(timezone.utc)
            timestamp = now.isoformat()
            topic = self.db.execute(
                "SELECT chat_id FROM topics WHERE topic_id=?", (topic_id,)
            ).fetchone()
            if topic is None or topic["chat_id"] != chat_id:
                raise self.error("stop topic binding mismatch")
            duplicate = self.db.execute(
                "SELECT request_id, topic_id, cancelled_queued_count, status "
                "FROM provider_stop_requests WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
            if duplicate is not None:
                if duplicate["topic_id"] != topic_id:
                    raise self.error("stop receipt topic mismatch")
                if prepare_notice:
                    self._notice(str(duplicate["request_id"]), None, now)
                return (
                    str(duplicate["request_id"]),
                    int(duplicate["cancelled_queued_count"]),
                    duplicate["status"] == "pending",
                )
            self.db.execute(
                "INSERT OR IGNORE INTO observed_messages "
                "(chat_id,message_id,observer_agent_id,observed_at) VALUES (?,?,'hub',?)",
                (chat_id, message_id, timestamp),
            )
            cancelled = self.jobs.cancel_unstarted_for_stop(topic_id, timestamp)
            active = self.db.execute(
                """SELECT agent_id FROM provider_jobs job WHERE topic_id=? AND (
                     status IN ('leased','executing') OR (status='indeterminate'
                     AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence e
                                     WHERE e.job_id=job.job_id)
                     AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions r
                                     WHERE r.job_id=job.job_id)))
                   ORDER BY created_at LIMIT 1""",
                (topic_id,),
            ).fetchone()
            pending = active is not None
            target = str(active["agent_id"]) if active is not None else target_agent_id
            request_id = str(uuid.uuid4())
            self.db.execute(
                """INSERT INTO provider_stop_requests
                   (request_id,topic_id,chat_id,message_id,target_agent_id,status,
                    cancelled_queued_count,created_at,completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    topic_id,
                    chat_id,
                    message_id,
                    target,
                    "pending" if pending else "completed",
                    cancelled,
                    timestamp,
                    None if pending else timestamp,
                ),
            )
            self.jobs.complete_finished_stops(topic_id, timestamp)
            if prepare_notice and (pending or cancelled):
                self._notice(request_id, None, now)
            return request_id, cancelled, pending

    def enqueue_notice(self, request_id: str, telegram_html: str) -> bool:
        with self.transaction():
            return self._notice(request_id, telegram_html, datetime.now(timezone.utc))

    def _notice(self, request_id: str, body: str | None, now: datetime) -> bool:
        existing = self.notices.notices_for_stop(request_id)
        if existing:
            return True
        request = self.db.execute(
            """SELECT stop.*, topics.thread_id FROM provider_stop_requests stop
               JOIN topics ON topics.topic_id=stop.topic_id WHERE stop.request_id=?""",
            (request_id,),
        ).fetchone()
        if request is None:
            raise self.error("emergency stop request does not exist")
        if request["status"] != "pending" and not request["cancelled_queued_count"]:
            candidate = self.jobs.stop_notice_job(request_id)
            if candidate is None:
                return False
        if body is None:
            held = self.db.execute(
                """SELECT COUNT(*) FROM provider_job_holds held
                   JOIN provider_jobs job ON job.job_id=held.job_id
                   WHERE job.topic_id=? AND held.decision='pending'
                     AND job.status IN ('queued','retry_wait')""",
                (request["topic_id"],),
            ).fetchone()[0]
            others = self.db.execute(
                """SELECT COUNT(*) FROM provider_jobs WHERE topic_id != ?
                   AND status IN ('queued','retry_wait','leased','executing')""",
                (request["topic_id"],),
            ).fetchone()[0]
            state = (
                "Прерывание запрошено; завершение ещё не подтверждено. "
                "При неизвестном исходе каталог останется заблокирован."
                if request["status"] == "pending"
                else "Активного выполнения нет."
            )
            body = (
                f"Остановка в теме {request['thread_id']}. {state}\n"
                f"Отменено в очереди: {request['cancelled_queued_count']}. "
                f"Ожидают решения владельца: {held}.\n"
                f"Задач в других темах без изменений: {others}. "
                "Проверить состояние: /status."
            )
        self.notices.prepare_notice_in_transaction(
            event_key=f"stop:{request_id}:requested",
            kind="stop_requested",
            stop_request_id=request_id,
            chat_id=int(request["chat_id"]),
            thread_id=int(request["thread_id"]),
            reply_to_message_id=int(request["message_id"]),
            telegram_html=body,
            now=now,
        )
        return True
