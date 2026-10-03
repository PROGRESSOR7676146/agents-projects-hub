"""Shared passive validation for accepted-turn activity and first notice delivery."""

from __future__ import annotations

import sqlite3

ACTIVITY_BINDING = (
    "lease_token",
    "topic_id",
    "project_id",
    "execution_scope",
    "chat_id",
    "thread_id",
    "session_id",
    "session_generation",
    "agent_id",
    "provider_thread_id",
    "provider_turn_id",
    "project_root",
)


def current_activity_binding(
    connection: sqlite3.Connection, job_id: str, token: str, timestamp: str
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT job.lease_token,job.topic_id,topic.project_id,"
        "COALESCE(topic.execution_scope,'project:'||topic.project_id) AS execution_scope,"
        "job.chat_id,topic.thread_id,job.session_id,"
        "job.session_generation,job.agent_id,checkpoint.provider_thread_id,"
        "checkpoint.provider_turn_id,checkpoint.project_root FROM provider_jobs job "
        "JOIN topics topic ON topic.topic_id=job.topic_id AND topic.chat_id=job.chat_id "
        "JOIN agent_sessions session ON session.session_id=job.session_id "
        "AND session.topic_id=job.topic_id AND session.agent_id=job.agent_id "
        "AND session.generation=job.session_generation "
        "JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id=job.job_id "
        "AND checkpoint.provider_thread_id=session.provider_session_id "
        "WHERE job.job_id=? AND job.status='executing' AND job.lease_token=? "
        "AND job.lease_expires_at>? AND session.writer_mode='telegram' "
        "AND session.status IN ('active','satellite') "
        "AND checkpoint.provider_turn_id IS NOT NULL AND checkpoint.completed_text IS NULL "
        "AND (COALESCE(topic.execution_scope,'project:'||topic.project_id)="
        "'root:'||checkpoint.project_root OR "
        "COALESCE(topic.execution_scope,'project:'||topic.project_id)='project:'||topic.project_id)",
        (job_id, token, timestamp),
    ).fetchone()
    return row


def activity_notice_is_current(
    connection: sqlite3.Connection,
    *,
    job_id: str | None,
    kind: str,
    event_key: str,
    chat_id: int,
    thread_id: int,
    created_at: str,
    timestamp: str,
) -> bool:
    if job_id is None:
        return False
    tables = connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('task_activity','task_activity_entries')"
    ).fetchone()[0]
    if tables != 2:
        return False
    bound = connection.execute("SELECT * FROM task_activity WHERE job_id=?", (job_id,)).fetchone()
    if bound is None:
        return False
    live = current_activity_binding(connection, job_id, bound["lease_token"], timestamp)
    if live is None or any(bound[key] != live[key] for key in ACTIVITY_BINDING):
        return False
    if (chat_id, thread_id) != (bound["chat_id"], bound["thread_id"]):
        return False
    if kind == "no_progress":
        return (
            bound["mode"] != "approval"
            and bound["notified_episode"] == bound["episode"]
            and bound["last_meaningful_at"] <= created_at
            and event_key == f"activity:{job_id}:no-progress:{bound['episode']}"
        )
    prefix = f"activity:{job_id}:approval:"
    if kind != "approval_wait" or not event_key.startswith(prefix) or bound["mode"] != "approval":
        return False
    return (
        connection.execute(
            "SELECT 1 FROM task_activity_entries WHERE job_id=? AND kind='approval' "
            "AND identity=? AND state='pending'",
            (job_id, event_key[len(prefix) :]),
        ).fetchone()
        is not None
    )
