from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

LATEST_SCHEMA_VERSION = 6


MIGRATION_1 = """
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
    writer_mode TEXT NOT NULL DEFAULT 'telegram'
        CHECK(writer_mode IN ('telegram', 'terminal')),
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


MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS runtime_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('info', 'warning', 'error')),
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runtime_events_created_at
ON runtime_events(created_at DESC);
CREATE TABLE IF NOT EXISTS worktree_lanes (
    lane_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    topic_id INTEGER REFERENCES topics(topic_id),
    worktree_path TEXT NOT NULL UNIQUE,
    branch_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS turn_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS turn_dispatches_status
ON turn_dispatches(status, updated_at);
"""


MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS alert_deliveries (
    alert_key TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);
"""


MIGRATION_5 = """
ALTER TABLE worktree_lanes ADD COLUMN cleaned_at TEXT;
"""


MIGRATION_6 = """
CREATE TABLE IF NOT EXISTS visible_context_cursors (
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    observer_agent_id TEXT NOT NULL,
    last_turn_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(topic_id, observer_agent_id)
);
"""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    previous_version: int
    current_version: int
    backup_path: Path | None


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_database(source: Path, destination: Path | None = None) -> Path:
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination is None:
        destination = source.with_name(f"{source.name}.backup-{_timestamp()}")
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise FileExistsError(destination)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        check = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise RuntimeError("SQLite backup integrity check failed")
    except Exception:
        destination_connection.close()
        source_connection.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        try:
            destination_connection.close()
        except sqlite3.Error:
            pass
        try:
            source_connection.close()
        except sqlite3.Error:
            pass
    os.chmod(destination, 0o600)
    return destination


def _ensure_legacy_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")}
    if columns and "writer_mode" not in columns:
        connection.execute(
            "ALTER TABLE agent_sessions ADD COLUMN writer_mode TEXT NOT NULL DEFAULT 'telegram'"
        )


def migrate_connection(connection: sqlite3.Connection) -> tuple[int, int]:
    previous = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if previous > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {previous} is newer than supported {LATEST_SCHEMA_VERSION}"
        )
    if previous < 1:
        connection.executescript(MIGRATION_1)
        _ensure_legacy_columns(connection)
        connection.execute("PRAGMA user_version = 1")
    if previous < 2:
        connection.executescript(MIGRATION_2)
        connection.execute("PRAGMA user_version = 2")
    if previous < 3:
        connection.executescript(MIGRATION_3)
        connection.execute("PRAGMA user_version = 3")
    if previous < 4:
        connection.executescript(MIGRATION_4)
        connection.execute("PRAGMA user_version = 4")
    if previous < 5:
        connection.executescript(MIGRATION_5)
        connection.execute("PRAGMA user_version = 5")
    if previous < 6:
        connection.executescript(MIGRATION_6)
        connection.execute("PRAGMA user_version = 6")
    connection.commit()
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    return previous, current


@contextmanager
def _migration_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.migration.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def migrate_database(path: Path, *, create_backup: bool = True) -> MigrationResult:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _migration_lock(path):
        existed = path.exists() and path.stat().st_size > 0
        backup_path: Path | None = None
        connection = sqlite3.connect(path)
        try:
            previous = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if existed and previous < LATEST_SCHEMA_VERSION and create_backup:
                connection.close()
                backup_path = backup_database(path)
                connection = sqlite3.connect(path)
            _, current = migrate_connection(connection)
            check = connection.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                raise RuntimeError("SQLite integrity check failed after migration")
        except Exception:
            connection.close()
            if backup_path is not None:
                shutil.copy2(backup_path, path)
                os.chmod(path, 0o600)
            raise
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    os.chmod(path, 0o600)
    return MigrationResult(previous, current, backup_path)
