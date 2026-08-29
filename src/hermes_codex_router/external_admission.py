from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PendingHandoff:
    handoff_id: str
    source_agent_id: str
    text: str


def is_active_agent(
    state_path: Path,
    chat_id: int,
    thread_id: int,
    *,
    agent_id: str,
) -> bool:
    """Fail-closed admission lookup for externally managed agent adapters."""
    if chat_id >= 0 or thread_id <= 0 or not agent_id:
        return False
    path = state_path.expanduser().resolve()
    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
        row = connection.execute(
            "SELECT active_agent_id FROM topics WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        return row is not None and row[0] == agent_id
    except (OSError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()


def peek_pending_handoff(
    state_path: Path,
    chat_id: int,
    thread_id: int,
    *,
    target_agent_id: str,
) -> PendingHandoff | None:
    path = state_path.expanduser().resolve()
    if not path.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
        row = connection.execute(
            """SELECT h.handoff_id, h.source_agent_id, h.text
               FROM pending_handoffs h
               JOIN topics t ON t.topic_id = h.topic_id
               WHERE t.chat_id = ? AND t.thread_id = ? AND h.target_agent_id = ?""",
            (chat_id, thread_id, target_agent_id),
        ).fetchone()
        if row is None:
            return None
        return PendingHandoff(str(row[0]), str(row[1]), str(row[2]))
    except (OSError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            connection.close()


def consume_pending_handoff(state_path: Path, handoff_id: str) -> bool:
    path = state_path.expanduser().resolve()
    if not path.is_file() or not handoff_id:
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=0.2)
        with connection:
            cursor = connection.execute(
                "DELETE FROM pending_handoffs WHERE handoff_id = ?", (handoff_id,)
            )
        return cursor.rowcount == 1
    except (OSError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()


def record_external_turn(
    state_path: Path,
    *,
    chat_id: int,
    thread_id: int,
    agent_id: str,
    provider_session_id: str | None,
    model: str | None,
    provider: str,
    user_excerpt: str,
    response_excerpt: str,
) -> bool:
    """Persist a bounded visible turn for active or satellite agents."""
    path = state_path.expanduser().resolve()
    if not path.is_file() or not user_excerpt.strip() or not response_excerpt.strip():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=0.5)
        with connection:
            row = connection.execute(
                "SELECT topic_id FROM topics WHERE chat_id = ? AND thread_id = ?",
                (chat_id, thread_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """INSERT INTO external_turn_excerpts
                   (topic_id, agent_id, provider_session_id, model, provider,
                    user_excerpt, response_excerpt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    row[0],
                    agent_id,
                    (provider_session_id or "")[:200],
                    (model or "unknown")[:200],
                    provider[:200],
                    user_excerpt.strip()[:500],
                    response_excerpt.strip()[:500],
                ),
            )
            connection.execute(
                """DELETE FROM external_turn_excerpts
                   WHERE topic_id = ? AND agent_id = ? AND turn_id NOT IN (
                     SELECT turn_id FROM external_turn_excerpts
                     WHERE topic_id = ? AND agent_id = ?
                     ORDER BY turn_id DESC LIMIT 20
                   )""",
                (row[0], agent_id, row[0], agent_id),
            )
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()
