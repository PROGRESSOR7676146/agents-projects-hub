"""Notice-bound passive retry reports; no provider or execution mutation authority."""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .delivery_hold_predicates import outbox_delivery_hold_released
from .task_activity_binding import ACTIVITY_BINDING, current_activity_binding
from .task_lifecycle import TaskLifecycleNotice

if TYPE_CHECKING:
    from .state import HubState


@dataclass(frozen=True, slots=True)
class RetryReport:
    notice: TaskLifecycleNotice | None
    created: bool


class WorkRetryState:
    """Use one HubState-owned transaction for input receipt and delivery snapshot."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.db = state._connection

    def _source(self, chat_id: int, thread_id: int, notice_message_id: int) -> str | None:
        rows = self.db.execute(
            "SELECT DISTINCT notice.job_id FROM task_lifecycle_notices notice "
            "JOIN provider_jobs job ON job.job_id=notice.job_id "
            "JOIN topics topic ON topic.topic_id=job.topic_id "
            "WHERE notice.status='delivered' AND notice.telegram_message_id=? "
            "AND notice.chat_id=? AND notice.thread_id=? "
            "AND job.chat_id=notice.chat_id AND topic.chat_id=notice.chat_id "
            "AND topic.thread_id=notice.thread_id LIMIT 2",
            (notice_message_id, chat_id, thread_id),
        ).fetchall()
        return str(rows[0]["job_id"]) if len(rows) == 1 else None

    def _text(self, row: sqlite3.Row, root: Path, timestamp: str) -> str:
        common = " No new run was started; retry did not change task or writer ownership."
        if (
            row["session_topic"] != row["topic_id"]
            or row["session_agent"] != row["agent_id"]
            or row["current_generation"] != row["session_generation"]
            or row["session_status"] not in {"active", "satellite"}
            or row["writer_mode"] != "telegram"
            or row["execution_scope"] != "root:" + str(root)
            or (
                row["provider_session_id"] is not None
                and row["provider_session_id"] != row["current_thread"]
            )
        ):
            return (
                "At retry time, this task's session or project binding had changed. Inspect /status."
                + common
            )
        status = row["status"]
        delivery = self.db.execute(
            f"SELECT {outbox_delivery_hold_released('o')} AS released FROM telegram_outbox o "
            "WHERE o.job_id=? AND o.status='unknown'",
            (row["job_id"],),
        ).fetchone()
        if delivery is not None:
            return (
                "At retry time, Telegram delivery was unknown. "
                + (
                    "The owner allowed queue continuation without confirmed delivery. "
                    if delivery["released"]
                    else "A delivery hold remains; inspect the local delivery-hold preview. "
                )
                + "Saved execution evidence and independent safety boundaries remain unchanged; "
                "the old message is not automatically resent. Inspect /status." + common
            )
        if status == "executing":
            if row["lease_expires_at"] is None or row["lease_expires_at"] <= timestamp:
                return (
                    "At retry time, current execution was unconfirmed. Existing exclusion was retained; inspect /status."
                    + common
                )
            activity = self.db.execute(
                "SELECT * FROM task_activity WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            live = current_activity_binding(self.db, row["job_id"], row["lease_token"], timestamp)
            if (
                activity is not None
                and live is not None
                and all(activity[key] == live[key] for key in ACTIVITY_BINDING)
                and activity["mode"] == "approval"
            ):
                return (
                    "At retry time, the same request was waiting for human approval. Open its Codex/tlive permission request to allow or deny it, or use /stop here. Retry does not approve it."
                    + common
                )
            return (
                "At retry time, the same request was already being processed. Wait for its existing result, or use /stop here."
                + common
            )
        if status in {"queued", "retry_wait", "leased"}:
            if row["hold_decision"] == "pending":
                return (
                    "At retry time, the same request was paused and required your decision. "
                    "Confirm or cancel it from its held-request notice, or inspect /status. "
                    "Retry did not confirm or release it." + common
                )
            return (
                "At retry time, the same request was already queued or preparing to run. Wait for its existing result; inspect /status or use /stop here."
                + common
            )
        if status == "indeterminate":
            if row["terminal_status"] is not None or row["owner_resolution"] is not None:
                return (
                    "At retry time, the prior outcome already had terminal evidence or an owner resolution. "
                    "This notice does not authorize a continuation; inspect /status and its failure notice."
                    + common
                )
            return (
                "At retry time, the task outcome was unconfirmed. Existing exclusion was retained; inspect /status and the task's failure notice."
                + common
            )
        if status in {"result_ready", "completed"}:
            return (
                "At retry time, the task result was already prepared. Wait for delivery or inspect /status."
                + common
            )
        return (
            "At retry time, this task had already stopped. This retry did not authorize a continuation; inspect /status and its failure notice."
            + common
        )

    def report_from_notice(
        self,
        *,
        chat_id: int,
        thread_id: int,
        notice_message_id: int,
        reply_message_id: int,
        canonical_root: Path,
        now: datetime,
    ) -> RetryReport | None:
        notices = self.state.task_notices
        notices._integer(chat_id, "chat identity", positive=False)
        for value in (thread_id, notice_message_id, reply_message_id):
            notices._integer(value, "retry identity")
        timestamp = notices._time(now)
        root = canonical_root.resolve(strict=True)
        with self.state._immediate_transaction():
            event_key = f"retry:{chat_id}:{reply_message_id}"
            prior = self.db.execute(
                "SELECT notice_id FROM task_lifecycle_notices WHERE event_key=?", (event_key,)
            ).fetchone()
            if prior is not None:
                original = notices.get_notice(prior["notice_id"])
                if (original.chat_id, original.thread_id, original.reply_to_message_id) != (
                    chat_id,
                    thread_id,
                    reply_message_id,
                ):
                    return RetryReport(None, False)
                return RetryReport(original, False)
            if self.state.message_already_observed(chat_id, reply_message_id):
                return RetryReport(None, False)
            source = self._source(chat_id, thread_id, notice_message_id)
            if source is None:
                return None
            row = self.db.execute(
                "SELECT job.*,topic.execution_scope,session.topic_id AS session_topic,"
                "session.agent_id AS session_agent,session.status AS session_status,"
                "session.generation AS current_generation,session.writer_mode,"
                "session.provider_session_id AS current_thread,terminal.terminal_status,"
                "resolution.resolution AS owner_resolution,hold.decision AS hold_decision "
                "FROM provider_jobs job "
                "JOIN topics topic ON topic.topic_id=job.topic_id "
                "LEFT JOIN agent_sessions session ON session.session_id=job.session_id "
                "LEFT JOIN provider_turn_terminal_evidence terminal ON terminal.job_id=job.job_id "
                "LEFT JOIN provider_job_resolutions resolution ON resolution.job_id=job.job_id "
                "LEFT JOIN provider_job_holds hold ON hold.job_id=job.job_id "
                "WHERE job.job_id=?",
                (source,),
            ).fetchone()
            assert row is not None
            text = self._text(row, root, timestamp)
            notice, created = notices.prepare_notice_in_transaction(
                event_key=event_key,
                kind="retry_report",
                job_id=source,
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_message_id,
                telegram_html=html.escape(text),
                now=now,
            )
            self.db.execute(
                "INSERT INTO observed_messages(chat_id,message_id,observer_agent_id,observed_at) "
                "VALUES(?,?,'hub',?)",
                (chat_id, reply_message_id, timestamp),
            )
            return RetryReport(notice, created)
