"""Passive prepared-checkpoint checks, distinct from accepted-turn authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .state_errors import StateError
from .task_activity_binding import activity_notice_is_current

PREPARED_BINDING = (
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
    "project_root",
    "codex_permission_profile",
)


def current_prepared_binding(
    db: sqlite3.Connection,
    job_id: str,
    token: str,
    timestamp: str,
    selected_profile: Callable[[], str | None] | None,
) -> sqlite3.Row | None:
    if selected_profile is None:
        return None
    try:
        profile = selected_profile()
    except StateError:
        return None
    return db.execute(
        "SELECT job.job_id,job.lease_token,job.topic_id,topic.project_id,"
        "COALESCE(topic.execution_scope,'project:'||topic.project_id) AS execution_scope,"
        "job.chat_id,topic.thread_id,job.session_id,job.session_generation,job.agent_id,"
        "checkpoint.provider_thread_id,checkpoint.provider_turn_id,checkpoint.project_root,"
        "checkpoint.codex_permission_profile FROM provider_jobs job "
        "JOIN topics topic ON topic.topic_id=job.topic_id AND topic.chat_id=job.chat_id "
        "JOIN agent_sessions session ON session.session_id=job.session_id "
        "AND session.topic_id=job.topic_id AND session.agent_id=job.agent_id "
        "AND session.generation=job.session_generation "
        "JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id=job.job_id "
        "AND checkpoint.provider_thread_id=session.provider_session_id "
        "WHERE job.job_id=? AND job.status='executing' AND job.lease_token=? "
        "AND job.lease_expires_at>? AND session.writer_mode='telegram' "
        "AND session.status IN ('active','satellite') AND checkpoint.completed_text IS NULL "
        "AND checkpoint.codex_permission_profile IS job.codex_permission_profile "
        "AND job.codex_permission_profile IS session.codex_permission_profile "
        "AND session.codex_permission_profile IS ? "
        "AND (COALESCE(topic.execution_scope,'project:'||topic.project_id)="
        "'root:'||checkpoint.project_root OR "
        "COALESCE(topic.execution_scope,'project:'||topic.project_id)='project:'||topic.project_id)",
        (job_id, token, timestamp, profile),
    ).fetchone()


def epoch_is_current(db: sqlite3.Connection, scope: sqlite3.Row) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM preacceptance_runtime_epochs WHERE slot_key=? AND epoch=? "
            "AND instance_token=? AND agent_id=?",
            (scope["slot_key"], scope["epoch"], scope["instance_token"], scope["agent_id"]),
        ).fetchone()
        is not None
    )


def preacceptance_notice_is_current(
    db: sqlite3.Connection,
    *,
    job_id: str | None,
    event_key: str,
    chat_id: int,
    thread_id: int,
    created_at: str,
    timestamp: str,
    selected_profile: Callable[[], str | None] | None,
) -> bool | None:
    """None means no early provenance; stale provenance must not fall through."""
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preacceptance_requests'"
        ).fetchone()
        is None
    ):
        return None
    row = db.execute(
        "SELECT scopes.*,requests.identity,requests.observed_turn_id,requests.item_identity,"
        "requests.category,requests.state AS request_state FROM preacceptance_requests requests "
        "JOIN preacceptance_scopes scopes ON scopes.scope_id=requests.scope_id WHERE event_key=?",
        (event_key,),
    ).fetchone()
    if row is None:
        return None
    if (
        job_id != row["job_id"]
        or row["state"] == "retired"
        or row["request_state"] != "pending"
        or (chat_id, thread_id) != (row["chat_id"], row["thread_id"])
    ):
        return False
    live = current_prepared_binding(
        db, row["job_id"], row["lease_token"], timestamp, selected_profile
    )
    if live is None or any(row[key] != live[key] for key in PREPARED_BINDING):
        return False
    if row["state"] == "open":
        return epoch_is_current(db, row) and live["provider_turn_id"] in (
            None,
            row["observed_turn_id"],
        )
    if live["provider_turn_id"] != row["observed_turn_id"]:
        return False
    matching = db.execute(
        "SELECT 1 FROM task_activity_entries WHERE job_id=? AND kind='approval' AND identity=? "
        "AND item_identity=? AND category=? AND state='pending'",
        (row["job_id"], row["identity"], row["item_identity"], row["category"]),
    ).fetchone()
    return matching is not None and activity_notice_is_current(
        db,
        job_id=job_id,
        kind="approval_wait",
        event_key=event_key,
        chat_id=chat_id,
        thread_id=thread_id,
        created_at=created_at,
        timestamp=timestamp,
    )
