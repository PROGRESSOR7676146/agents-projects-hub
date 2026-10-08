"""SQLite-only current bindings and evidence reads for Claude quiet notices."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .claude_permission_binding import binding_digest, binding_snapshot
from .stop_coverage import STOP_COVERS_JOB_SQL

CLAUDE_ACTIVITY_BINDING = (
    "lease_token",
    "topic_id",
    "project_id",
    "execution_scope",
    "chat_id",
    "thread_id",
    "session_id",
    "session_generation",
    "agent_id",
    "native_session_id",
    "project_root",
    "provider_started_at",
)


def current_claude_binding(
    db: sqlite3.Connection, job_id: str, token: str, timestamp: str
) -> sqlite3.Row | None:
    return db.execute(
        "SELECT job.job_id,job.lease_token,job.topic_id,topic.project_id,"
        "COALESCE(topic.execution_scope,'project:'||topic.project_id) AS execution_scope,"
        "job.chat_id,topic.thread_id,job.session_id,job.session_generation,job.agent_id,"
        "checkpoint.provider_thread_id AS native_session_id,checkpoint.project_root,"
        "job.provider_started_at FROM provider_jobs job "
        "JOIN topics topic ON topic.topic_id=job.topic_id AND topic.chat_id=job.chat_id "
        "JOIN agent_sessions session ON session.session_id=job.session_id "
        "AND session.topic_id=job.topic_id AND session.agent_id=job.agent_id "
        "AND session.generation=job.session_generation "
        "JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id=job.job_id "
        "AND checkpoint.provider_thread_id=session.provider_session_id "
        "WHERE job.job_id=? AND job.status='executing' AND job.lease_token=? "
        "AND job.lease_expires_at>? AND job.provider_started_at IS NOT NULL "
        "AND session.writer_mode='telegram' AND session.status IN ('active','satellite') "
        "AND checkpoint.provider_turn_id IS NULL AND checkpoint.completed_text IS NULL "
        "AND (job.provider_session_id IS NULL OR job.provider_session_id=checkpoint.provider_thread_id) "
        "AND COALESCE(topic.execution_scope,'project:'||topic.project_id) "
        "IN ('root:'||checkpoint.project_root,'project:'||topic.project_id) "
        f"AND NOT EXISTS(SELECT 1 FROM provider_stop_requests stop WHERE {STOP_COVERS_JOB_SQL} "
        "AND (stop.status='pending' OR stop.created_at>=job.provider_started_at))",
        (job_id, token, timestamp),
    ).fetchone()


def latest_claude_visible(db: sqlite3.Connection, job_id: str) -> tuple[int, str | None] | None:
    row = db.execute(
        "SELECT sequence,created_at,phase FROM provider_visible_items "
        "WHERE job_id=? ORDER BY sequence DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        return 0, None
    if row["phase"] != "unknown" or row["sequence"] <= 0:
        return None
    return row["sequence"], row["created_at"]


@dataclass(frozen=True, slots=True)
class ClaudePermissionSnapshot:
    launch_epoch: str | None
    digest: str
    waiting: bool


def claude_permission_snapshot(
    db: sqlite3.Connection, bound: sqlite3.Row, timestamp: str
) -> ClaudePermissionSnapshot | None:
    mode = db.execute(
        "SELECT mode.mode,mode.home_digest,launch.launch_epoch,launch.binding_digest,launch.status "
        "FROM claude_permission_session_modes mode "
        "LEFT JOIN claude_permission_launches launch ON launch.job_id=? "
        "WHERE mode.provider_session_id=?",
        (bound["job_id"], bound["native_session_id"]),
    ).fetchone()
    if mode is None:
        return None
    epoch, waiting = mode["launch_epoch"], False
    rows: list[list[object]] = []
    if mode["mode"] == "text_only":
        if epoch is not None:
            return None
    elif mode["mode"] == "file_tools":
        raw = binding_snapshot(db, bound["job_id"])
        if epoch is None or mode["status"] != "active" or raw is None:
            return None
        if mode["binding_digest"] != binding_digest(
            bound["job_id"],
            bound["lease_token"],
            bound["native_session_id"],
            bound["project_root"],
            raw,
        ):
            return None
        if (
            db.execute(
                "SELECT 1 FROM provider_stop_requests WHERE topic_id=? "
                "AND (status='pending' OR created_at>=?) LIMIT 1",
                (bound["topic_id"], bound["provider_started_at"]),
            ).fetchone()
            is not None
        ):
            return None
        stored = db.execute(
            "SELECT request_nonce,status,expires_at,consumed_at FROM claude_permission_requests "
            "WHERE launch_epoch=? ORDER BY request_nonce LIMIT 129",
            (epoch,),
        ).fetchall()
        if len(stored) > 128:
            return None
        now_ms = int(datetime.fromisoformat(timestamp).timestamp() * 1000)
        rows = [list(row) for row in stored]
        waiting = any(row["status"] == "pending" and row["expires_at"] > now_ms for row in stored)
    else:
        return None
    encoded = json.dumps([mode["mode"], mode["home_digest"], epoch, rows], separators=(",", ":"))
    return ClaudePermissionSnapshot(epoch, hashlib.sha256(encoded.encode()).hexdigest(), waiting)


def claude_activity_notice_is_current(
    db: sqlite3.Connection,
    *,
    job_id: str | None,
    event_key: str,
    chat_id: int,
    thread_id: int,
    created_at: str,
    timestamp: str,
) -> bool:
    if job_id is None:
        return False
    observed = db.execute(
        "SELECT * FROM claude_activity_observations WHERE job_id=? AND retired_at IS NULL",
        (job_id,),
    ).fetchone()
    if observed is None:
        return False
    live = current_claude_binding(db, job_id, observed["lease_token"], timestamp)
    if live is None or any(observed[key] != live[key] for key in CLAUDE_ACTIVITY_BINDING):
        return False
    visible = latest_claude_visible(db, job_id)
    permission = claude_permission_snapshot(db, live, timestamp)
    return (
        (chat_id, thread_id) == (observed["chat_id"], observed["thread_id"])
        and observed["notified_episode"] == observed["episode"]
        and event_key == f"claude-observation:{job_id}:no-progress:{observed['episode']}"
        and observed["quiet_since_at"] <= created_at
        and visible is not None
        and visible[0] == observed["last_visible_sequence"]
        and permission is not None
        and not permission.waiting
        and permission.launch_epoch == observed["permission_launch_epoch"]
        and permission.digest == observed["permission_snapshot_digest"]
        and not observed["permission_waiting"]
    )
