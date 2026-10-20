"""Delivery-blocker observations, never delivery or execution authority."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

from .delivery_hold_predicates import job_blocks_topic_fifo

if TYPE_CHECKING:
    from .task_lifecycle import TaskLifecycleState

DELIVERY_WAIT_EXPLANATION = (
    "The earlier result is saved, but its Telegram delivery is unconfirmed. "
    "At this observation, later work in this topic is blocked. The owner can "
    "investigate the saved delivery, or explicitly continue without confirmed delivery "
    "through the local delivery-hold preview/apply. This queue-only decision retains "
    "session and writer restrictions; separate delivery-control consent can reconcile "
    "those delivery waits. Hub will not resend the result or repeat provider work "
    "automatically. Check /status for the current state; /stop does not reconcile this delivery."
)


def _blocking_result() -> str:
    return (
        "o.status='unknown' AND j.status='result_ready' "
        "AND o.sender_agent_id=j.agent_id AND o.chat_id=j.chat_id "
        "AND t.chat_id=o.chat_id AND t.thread_id=o.thread_id "
        "AND t.thread_id>0 AND j.message_id>0 AND o.chat_id!=0 "
        "AND EXISTS (SELECT 1 FROM provider_job_results r WHERE r.job_id=j.job_id) "
        f"AND {job_blocks_topic_fifo('j')}"
    )


def prepare_delivery_waits(
    connection: sqlite3.Connection,
    notices: TaskLifecycleState,
    sender_agent_ids: tuple[str, ...],
    *,
    now: datetime,
) -> int:
    """One bounded pass in the facade-owned transaction; deduplicate before send."""
    placeholders = ",".join("?" for _ in sender_agent_ids)
    rows = connection.execute(
        "SELECT o.outbox_id,j.job_id,o.chat_id,o.thread_id,j.message_id "
        "FROM telegram_outbox o JOIN provider_jobs j ON j.job_id=o.job_id "
        "JOIN topics t ON t.topic_id=j.topic_id "
        f"WHERE {_blocking_result()} AND o.sender_agent_id IN ({placeholders}) "
        "AND NOT EXISTS (SELECT 1 FROM task_lifecycle_notices n "
        "WHERE n.event_key='delivery-wait:' || o.outbox_id) "
        "ORDER BY o.created_at,o.outbox_id LIMIT 32",
        sender_agent_ids,
    ).fetchall()
    for row in rows:
        notices.prepare_notice_in_transaction(
            event_key=f"delivery-wait:{row['outbox_id']}",
            kind="delivery_wait",
            job_id=str(row["job_id"]),
            chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]),
            reply_to_message_id=int(row["message_id"]),
            telegram_html=DELIVERY_WAIT_EXPLANATION,
            now=now,
        )
    return len(rows)


def delivery_wait_notice_is_current(
    connection: sqlite3.Connection,
    *,
    event_key: str,
    job_id: str | None,
    chat_id: int,
    thread_id: int,
) -> bool:
    """Suppress an unattempted notice after exact receipt or owner reconciliation."""
    return (
        connection.execute(
            "SELECT 1 FROM telegram_outbox o JOIN provider_jobs j ON j.job_id=o.job_id "
            "JOIN topics t ON t.topic_id=j.topic_id "
            f"WHERE {_blocking_result()} AND 'delivery-wait:' || o.outbox_id=? "
            "AND j.job_id=? AND o.chat_id=? AND o.thread_id=?",
            (event_key, job_id, chat_id, thread_id),
        ).fetchone()
        is not None
    )
