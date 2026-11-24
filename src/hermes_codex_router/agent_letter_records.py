"""Agent letter rows committed inside a provider result transaction (ADR 0112).

Kept apart from the store so the state facade can call it without an import
cycle; it only writes agent mail tables on the caller's open transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .agent_mail import MailRoute


def letter_time(value: datetime) -> str:
    """One comparable UTC form for every agent mail timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("agent mail time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class AgentLetterDraft:
    letter_id: str
    route: MailRoute
    target_topic_id: int
    file_name: str
    body: str
    body_sha256: str
    caption_html: str
    source_label: str


# The whole route, both endpoints included, must still be configured and
# both endpoint topics must still belong to the route's projects.
ROUTE_AUTHORIZED_SQL = """
    SELECT 1 FROM agent_mail_routes routes
    JOIN topics source ON source.chat_id = routes.source_chat_id
     AND source.thread_id = routes.source_thread_id
     AND source.project_id = routes.source_project_id
    JOIN topics target ON target.chat_id = routes.target_chat_id
     AND target.thread_id = routes.target_thread_id
     AND target.project_id = routes.target_project_id
    WHERE routes.alias = ? AND routes.source_project_id = ? AND routes.source_chat_id = ?
      AND routes.source_thread_id = ? AND routes.target_project_id = ?
      AND routes.target_chat_id = ? AND routes.target_thread_id = ?
"""


def route_parameters(route: MailRoute) -> tuple[object, ...]:
    return (
        route.alias,
        route.source_project_id,
        route.source_chat_id,
        route.source_thread_id,
        route.target_project_id,
        route.target_chat_id,
        route.target_thread_id,
    )


def insert_letter_in_transaction(
    connection: sqlite3.Connection,
    job_row: sqlite3.Row,
    draft: AgentLetterDraft,
    timestamp: str,
) -> None:
    """Commit one letter and its pending display inside the result transaction.

    A route or endpoint that changed after the worker validated the letter
    still commits the letter, as a rejected display with a source notice, so
    the answer that already says "queued" is corrected instead of lost.
    """
    current = letter_time(datetime.fromisoformat(timestamp))
    route = draft.route
    connection.execute(
        """INSERT INTO agent_letters
           (letter_id, source_job_id, source_topic_id, source_agent_id, route_alias,
            source_project_id, source_chat_id, source_thread_id, target_project_id,
            target_chat_id, target_thread_id, target_topic_id, file_name, body,
            body_sha256, caption_html, source_label, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            draft.letter_id,
            job_row["job_id"],
            job_row["topic_id"],
            job_row["agent_id"],
            route.alias,
            route.source_project_id,
            route.source_chat_id,
            route.source_thread_id,
            route.target_project_id,
            route.target_chat_id,
            route.target_thread_id,
            draft.target_topic_id,
            draft.file_name,
            draft.body,
            draft.body_sha256,
            draft.caption_html,
            draft.source_label,
            current,
        ),
    )
    authorized = (
        connection.execute(ROUTE_AUTHORIZED_SQL, route_parameters(route)).fetchone() is not None
        and (route.source_chat_id, route.source_thread_id)
        == (job_row["chat_id"], job_row["thread_id"])
        and connection.execute(
            "SELECT 1 FROM topics WHERE topic_id = ? AND chat_id = ? AND thread_id = ?",
            (draft.target_topic_id, route.target_chat_id, route.target_thread_id),
        ).fetchone()
        is not None
    )
    if authorized:
        connection.execute(
            """INSERT INTO agent_letter_deliveries
               (letter_id, kind, chat_id, thread_id, state, available_at)
               VALUES (?, 'display', ?, ?, 'pending', ?)""",
            (draft.letter_id, route.target_chat_id, route.target_thread_id, current),
        )
        return
    connection.execute(
        """INSERT INTO agent_letter_deliveries
           (letter_id, kind, chat_id, thread_id, state, available_at, error_code, completed_at)
           VALUES (?, 'display', ?, ?, 'rejected', ?, 'route_changed', ?)""",
        (draft.letter_id, route.target_chat_id, route.target_thread_id, current, current),
    )
    connection.execute(
        """INSERT INTO agent_letter_deliveries
           (letter_id, kind, chat_id, thread_id, state, available_at)
           VALUES (?, 'failure_notice', ?, ?, 'pending', ?)""",
        (draft.letter_id, route.source_chat_id, route.source_thread_id, current),
    )
