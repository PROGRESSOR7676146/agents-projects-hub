"""Dependency-neutral binding and stale-notice checks on caller-owned SQLite."""

from __future__ import annotations

import hashlib
import json
import sqlite3


def binding_snapshot(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT job.topic_id,job.chat_id,job.agent_id,job.session_id,job.session_generation,"
        "topic.project_id,topic.thread_id,topic.execution_scope,job.provider_started_at "
        "FROM provider_jobs job JOIN topics topic ON topic.topic_id=job.topic_id "
        "WHERE job.job_id=?",
        (job_id,),
    ).fetchone()


def binding_digest(job_id: str, token: str, native: str, root: str, row: sqlite3.Row) -> str:
    data = json.dumps(
        [job_id, token, native, root, *tuple(row)],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def permission_notice_is_current(
    connection: sqlite3.Connection,
    *,
    job_id: str | None,
    event_key: str,
    chat_id: int,
    thread_id: int,
    timestamp: str,
) -> bool:
    if job_id is None:
        return False
    row = connection.execute(
        "SELECT launch.binding_digest,job.lease_token,checkpoint.provider_thread_id,"
        "checkpoint.project_root FROM claude_permission_requests request "
        "JOIN claude_permission_launches launch ON launch.launch_epoch=request.launch_epoch "
        "JOIN provider_jobs job ON job.job_id=launch.job_id "
        "JOIN topics topic ON topic.topic_id=job.topic_id AND topic.chat_id=job.chat_id "
        "JOIN agent_sessions session ON session.session_id=job.session_id "
        "AND session.topic_id=job.topic_id AND session.agent_id=job.agent_id "
        "AND session.generation=job.session_generation "
        "JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id=job.job_id "
        "AND checkpoint.provider_thread_id=session.provider_session_id "
        "WHERE launch.job_id=? AND launch.status='active' AND request.status='pending' "
        "AND 'claude-permission:'||request.request_nonce=? "
        "AND request.expires_at>CAST((julianday(?)-2440587.5)*86400000 AS INTEGER) "
        "AND job.status='executing' AND job.lease_expires_at>? "
        "AND session.writer_mode='telegram' AND session.status IN ('active','satellite') "
        "AND checkpoint.completed_text IS NULL AND checkpoint.provider_turn_id IS NULL "
        "AND topic.chat_id=? AND topic.thread_id=? "
        "AND NOT EXISTS(SELECT 1 FROM provider_stop_requests stop WHERE stop.topic_id=job.topic_id "
        "AND (stop.status='pending' OR stop.created_at>=job.provider_started_at))",
        (job_id, event_key, timestamp, timestamp, chat_id, thread_id),
    ).fetchone()
    bound = binding_snapshot(connection, job_id)
    return (
        row is not None
        and bound is not None
        and row[0] == binding_digest(job_id, row[1], row[2], row[3], bound)
    )
