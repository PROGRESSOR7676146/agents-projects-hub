from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class StateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TopicRecord:
    topic_id: int
    project_id: str
    chat_id: int
    thread_id: int
    title: str
    active_agent_id: str | None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    topic_id: int
    agent_id: str
    generation: int
    status: str
    model: str
    effort: str
    provider_session_id: str | None
    terminal_name: str | None
    writer_mode: str


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    handoff_id: str
    topic_id: int
    target_agent_id: str
    source_agent_id: str
    text: str


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS topics (
    topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    active_agent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    agent_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'satellite', 'archived')),
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    provider_session_id TEXT,
    terminal_name TEXT,
    writer_mode TEXT NOT NULL DEFAULT 'telegram' CHECK(writer_mode IN ('telegram', 'terminal')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(topic_id, agent_id, generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_topic
ON agent_sessions(topic_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS one_satellite_session_per_agent
ON agent_sessions(topic_id, agent_id) WHERE status = 'satellite';
CREATE TABLE IF NOT EXISTS observed_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    observer_agent_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS bot_offsets (
    agent_id TEXT PRIMARY KEY,
    next_update_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed_callbacks (
    callback_id TEXT PRIMARY KEY,
    observer_agent_id TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_handoffs (
    handoff_id TEXT PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    target_agent_id TEXT NOT NULL,
    source_agent_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(topic_id, target_agent_id)
);
CREATE TABLE IF NOT EXISTS external_turn_excerpts (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    agent_id TEXT NOT NULL,
    provider_session_id TEXT,
    model TEXT,
    provider TEXT,
    user_excerpt TEXT NOT NULL,
    response_excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HubState:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path) -> "HubState":
        path = path.expanduser().resolve()
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            path.parent.chmod(0o700)
        connection = sqlite3.connect(path)
        os.chmod(path, 0o600)
        connection.executescript(SCHEMA)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        if "writer_mode" not in columns:
            connection.execute(
                "ALTER TABLE agent_sessions ADD COLUMN writer_mode TEXT NOT NULL DEFAULT 'telegram'"
            )
        connection.commit()
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _topic(row: sqlite3.Row) -> TopicRecord:
        return TopicRecord(
            topic_id=row["topic_id"],
            project_id=row["project_id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            title=row["title"],
            active_agent_id=row["active_agent_id"],
        )

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            topic_id=row["topic_id"],
            agent_id=row["agent_id"],
            generation=row["generation"],
            status=row["status"],
            model=row["model"],
            effort=row["effort"],
            provider_session_id=row["provider_session_id"],
            terminal_name=row["terminal_name"],
            writer_mode=row["writer_mode"],
        )

    def observe_topic(
        self,
        *,
        project_id: str,
        chat_id: int,
        thread_id: int,
        title: str,
    ) -> TopicRecord:
        if chat_id >= 0 or thread_id <= 0 or not title.strip():
            raise StateError("invalid Telegram topic identity")
        now = _now()
        with self._connection:
            existing = self._connection.execute(
                "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ?",
                (chat_id, thread_id),
            ).fetchone()
            if existing is not None and existing["project_id"] != project_id:
                raise StateError("Telegram topic is already bound to another project")
            if existing is None:
                self._connection.execute(
                    """INSERT INTO topics
                       (project_id, chat_id, thread_id, title, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (project_id, chat_id, thread_id, title.strip(), now, now),
                )
            else:
                self._connection.execute(
                    "UPDATE topics SET title = ?, updated_at = ? WHERE topic_id = ?",
                    (title.strip(), now, existing["topic_id"]),
                )
        row = self._connection.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        if row is None:
            raise StateError("failed to persist Telegram topic")
        return self._topic(row)

    def get_topic(self, topic_id: int) -> TopicRecord:
        row = self._connection.execute(
            "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown topic_id: {topic_id}")
        return self._topic(row)

    def find_topic(self, chat_id: int, thread_id: int) -> TopicRecord | None:
        row = self._connection.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        return None if row is None else self._topic(row)

    def active_agent_for_route(self, chat_id: int, thread_id: int) -> str | None:
        """Return the selected agent for an exact Telegram topic.

        External adapters use this narrow lookup as an admission decision. An
        unknown topic or a topic without an active agent fails closed.
        """
        row = self._connection.execute(
            "SELECT active_agent_id FROM topics WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        if row is None or not row["active_agent_id"]:
            return None
        return str(row["active_agent_id"])

    def active_session(self, topic_id: int) -> SessionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
            (topic_id,),
        ).fetchone()
        return None if row is None else self._session(row)

    def get_session(self, session_id: str) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown session_id: {session_id}")
        return self._session(row)

    def bind_provider_session(
        self, session_id: str, provider_session_id: str, terminal_name: str | None
    ) -> SessionRecord:
        if not provider_session_id.strip():
            raise StateError("provider session id is empty")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET provider_session_id = ?, terminal_name = ?, "
                "updated_at = ? WHERE session_id = ?",
                (provider_session_id, terminal_name, _now(), session_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def set_writer_mode(self, session_id: str, writer_mode: str) -> SessionRecord:
        if writer_mode not in {"telegram", "terminal"}:
            raise StateError("invalid writer mode")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE agent_sessions SET writer_mode = ?, updated_at = ? WHERE session_id = ?",
                (writer_mode, _now(), session_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"unknown session_id: {session_id}")
        return self.get_session(session_id)

    def stage_handoff(
        self,
        topic_id: int,
        *,
        target_agent_id: str,
        source_agent_id: str,
        text: str,
    ) -> HandoffRecord:
        self.get_topic(topic_id)
        bounded = text.strip()[:20000]
        if not target_agent_id or not source_agent_id or not bounded:
            raise StateError("invalid handoff")
        handoff_id = str(uuid.uuid4())
        with self._connection:
            self._connection.execute(
                """INSERT INTO pending_handoffs
                   (handoff_id, topic_id, target_agent_id, source_agent_id, text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(topic_id, target_agent_id) DO UPDATE SET
                     handoff_id = excluded.handoff_id,
                     source_agent_id = excluded.source_agent_id,
                     text = excluded.text,
                     created_at = excluded.created_at""",
                (
                    handoff_id,
                    topic_id,
                    target_agent_id,
                    source_agent_id,
                    bounded,
                    _now(),
                ),
            )
        return HandoffRecord(
            handoff_id, topic_id, target_agent_id, source_agent_id, bounded
        )

    def recent_external_context(
        self, topic_id: int, agent_id: str, *, limit: int = 8
    ) -> str | None:
        if limit <= 0 or limit > 20:
            raise StateError("invalid external context limit")
        rows = self._connection.execute(
            """SELECT user_excerpt, response_excerpt, model, provider
               FROM external_turn_excerpts
               WHERE topic_id = ? AND agent_id = ?
               ORDER BY turn_id DESC LIMIT ?""",
            (topic_id, agent_id, limit),
        ).fetchall()
        if not rows:
            return None
        parts: list[str] = []
        for row in reversed(rows):
            label = "/".join(
                value for value in (row["provider"], row["model"]) if value
            ) or agent_id
            parts.append(
                f"USER: {row['user_excerpt']}\n{label.upper()}: {row['response_excerpt']}"
            )
        return "\n\n".join(parts)

    def _next_generation(self, topic_id: int, agent_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 AS value "
            "FROM agent_sessions WHERE topic_id = ? AND agent_id = ?",
            (topic_id, agent_id),
        ).fetchone()
        return int(row["value"])

    def _insert_session(
        self,
        topic_id: int,
        agent_id: str,
        model: str,
        effort: str,
        status: str,
    ) -> SessionRecord:
        session_id = str(uuid.uuid4())
        generation = self._next_generation(topic_id, agent_id)
        now = _now()
        self._connection.execute(
            """INSERT INTO agent_sessions
               (session_id, topic_id, agent_id, generation, status, model, effort,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, topic_id, agent_id, generation, status, model, effort, now, now),
        )
        return self.get_session(session_id)

    def activate_agent(
        self, topic_id: int, agent_id: str, model: str, effort: str
    ) -> SessionRecord:
        self.get_topic(topic_id)
        now = _now()
        with self._connection:
            self._connection.execute(
                "UPDATE agent_sessions SET status = 'archived', updated_at = ? "
                "WHERE topic_id = ? AND (status = 'active' OR (agent_id = ? AND status = 'satellite'))",
                (now, topic_id, agent_id),
            )
            session = self._insert_session(topic_id, agent_id, model, effort, "active")
            self._connection.execute(
                "UPDATE topics SET active_agent_id = ?, updated_at = ? WHERE topic_id = ?",
                (agent_id, now, topic_id),
            )
        return self.get_session(session.session_id)

    def ensure_satellite(
        self, topic_id: int, agent_id: str, model: str, effort: str
    ) -> SessionRecord:
        topic = self.get_topic(topic_id)
        if topic.active_agent_id == agent_id:
            row = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
                (topic_id,),
            ).fetchone()
            if row is None:
                raise StateError("topic has active agent but no active session")
            return self._session(row)
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND agent_id = ? AND status = 'satellite'",
            (topic_id, agent_id),
        ).fetchone()
        if row is not None:
            return self._session(row)
        with self._connection:
            session = self._insert_session(topic_id, agent_id, model, effort, "satellite")
        return self.get_session(session.session_id)

    def new_active_session(self, topic_id: int) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
            (topic_id,),
        ).fetchone()
        if row is None:
            raise StateError("topic has no active session")
        previous = self._session(row)
        with self._connection:
            self._connection.execute(
                "UPDATE agent_sessions SET status = 'archived', updated_at = ? WHERE session_id = ?",
                (_now(), previous.session_id),
            )
            replacement = self._insert_session(
                topic_id, previous.agent_id, previous.model, previous.effort, "active"
            )
        return self.get_session(replacement.session_id)

    def new_all_sessions(self, topic_id: int) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE topic_id = ? AND status = 'active'",
            (topic_id,),
        ).fetchone()
        if row is None:
            raise StateError("topic has no active session")
        previous = self._session(row)
        with self._connection:
            self._connection.execute(
                "UPDATE agent_sessions SET status = 'archived', updated_at = ? "
                "WHERE topic_id = ? AND status != 'archived'",
                (_now(), topic_id),
            )
            replacement = self._insert_session(
                topic_id, previous.agent_id, previous.model, previous.effort, "active"
            )
        return self.get_session(replacement.session_id)

    def claim_message(self, chat_id: int, message_id: int, *, observer_agent_id: str) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO observed_messages VALUES (?, ?, ?, ?)",
                    (chat_id, message_id, observer_agent_id, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_bot_offset(self, agent_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT next_update_id FROM bot_offsets WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return None if row is None else int(row["next_update_id"])

    def set_bot_offset(self, agent_id: str, next_update_id: int) -> None:
        if next_update_id < 0:
            raise StateError("next update id cannot be negative")
        with self._connection:
            self._connection.execute(
                "INSERT INTO bot_offsets(agent_id, next_update_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET next_update_id = excluded.next_update_id, "
                "updated_at = excluded.updated_at",
                (agent_id, next_update_id, _now()),
            )

    def claim_callback(self, callback_id: str, *, observer_agent_id: str) -> bool:
        if not callback_id:
            raise StateError("callback id is empty")
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO observed_callbacks VALUES (?, ?, ?)",
                    (callback_id, observer_agent_id, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True
