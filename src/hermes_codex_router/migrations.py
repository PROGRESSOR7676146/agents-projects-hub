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

LATEST_SCHEMA_VERSION = 14


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


MIGRATION_7 = """
DROP INDEX IF EXISTS one_active_session_per_topic;
DROP INDEX IF EXISTS one_satellite_session_per_agent;
ALTER TABLE agent_sessions RENAME TO agent_sessions_before_local_writer;
CREATE TABLE agent_sessions (
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
        CHECK(writer_mode IN ('telegram', 'local', 'terminal')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(topic_id, agent_id, generation)
);
INSERT INTO agent_sessions (
    session_id, topic_id, agent_id, generation, status, model, effort,
    provider_session_id, terminal_name, writer_mode, created_at, updated_at
)
SELECT
    session_id, topic_id, agent_id, generation, status, model, effort,
    provider_session_id, terminal_name, writer_mode, created_at, updated_at
FROM agent_sessions_before_local_writer;
DROP TABLE agent_sessions_before_local_writer;
CREATE UNIQUE INDEX one_active_session_per_topic
ON agent_sessions(topic_id) WHERE status = 'active';
CREATE UNIQUE INDEX one_satellite_session_per_agent
ON agent_sessions(topic_id, agent_id) WHERE status = 'satellite';
"""


MIGRATION_8 = """
ALTER TABLE agent_sessions ADD COLUMN context_remaining_percent REAL;
"""


MIGRATION_9 = """
CREATE TABLE IF NOT EXISTS runtime_checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    integer_value INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


MIGRATION_10 = """
CREATE TABLE IF NOT EXISTS topic_queue_counters (
    topic_id INTEGER PRIMARY KEY REFERENCES topics(topic_id),
    next_sequence INTEGER NOT NULL CHECK(next_sequence > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_jobs (
    job_id TEXT PRIMARY KEY CHECK(length(job_id) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL UNIQUE
        CHECK(length(idempotency_key) BETWEEN 1 AND 256),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    topic_sequence INTEGER NOT NULL CHECK(topic_sequence > 0),
    agent_id TEXT NOT NULL CHECK(length(agent_id) BETWEEN 1 AND 64),
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL CHECK(session_generation > 0),
    provider_session_id TEXT CHECK(length(provider_session_id) <= 256),
    model TEXT NOT NULL CHECK(length(model) BETWEEN 1 AND 200),
    effort TEXT NOT NULL CHECK(length(effort) BETWEEN 1 AND 64),
    payload_version INTEGER NOT NULL DEFAULT 1 CHECK(payload_version = 1),
    payload_text TEXT NOT NULL CHECK(length(payload_text) BETWEEN 1 AND 20000),
    context_watermark INTEGER CHECK(context_watermark IS NULL OR context_watermark >= 0),
    handoff_id TEXT CHECK(length(handoff_id) <= 128),
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'leased', 'executing', 'retry_wait', 'result_ready',
        'completed', 'failed', 'cancelled', 'indeterminate'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts BETWEEN 1 AND 20),
    next_attempt_at TEXT,
    lease_owner TEXT CHECK(length(lease_owner) <= 128),
    lease_token TEXT CHECK(length(lease_token) <= 128),
    lease_expires_at TEXT,
    provider_started_at TEXT,
    error_class TEXT CHECK(length(error_class) <= 64),
    error_code TEXT CHECK(length(error_code) <= 128),
    error_detail TEXT CHECK(length(error_detail) <= 1000),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, message_id),
    UNIQUE(topic_id, topic_sequence),
    CHECK(
        (status IN ('leased', 'executing') AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status NOT IN ('leased', 'executing') AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS provider_jobs_agent_ready
ON provider_jobs(agent_id, status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS provider_jobs_topic_fifo
ON provider_jobs(topic_id, topic_sequence, status);
CREATE INDEX IF NOT EXISTS provider_jobs_stale_lease
ON provider_jobs(status, lease_expires_at);
CREATE TABLE IF NOT EXISTS provider_job_results (
    result_id TEXT PRIMARY KEY CHECK(length(result_id) BETWEEN 1 AND 128),
    job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    visible_response TEXT NOT NULL CHECK(length(visible_response) BETWEEN 1 AND 12000),
    provider_session_id TEXT CHECK(length(provider_session_id) <= 256),
    actual_model TEXT CHECK(length(actual_model) <= 200),
    safe_metadata_json TEXT CHECK(length(safe_metadata_json) <= 4000),
    context_watermark INTEGER CHECK(context_watermark IS NULL OR context_watermark >= 0),
    handoff_id TEXT CHECK(length(handoff_id) <= 128),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_outbox (
    outbox_id TEXT PRIMARY KEY CHECK(length(outbox_id) BETWEEN 1 AND 128),
    job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    sender_agent_id TEXT NOT NULL CHECK(length(sender_agent_id) BETWEEN 1 AND 64),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    telegram_html TEXT NOT NULL CHECK(length(telegram_html) BETWEEN 1 AND 12000),
    status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'delivered', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
    available_at TEXT NOT NULL,
    lease_owner TEXT CHECK(length(lease_owner) <= 128),
    lease_token TEXT CHECK(length(lease_token) <= 128),
    lease_expires_at TEXT,
    telegram_message_id INTEGER CHECK(telegram_message_id IS NULL OR telegram_message_id > 0),
    error_code TEXT CHECK(length(error_code) <= 128),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    CHECK(
        (status = 'sending' AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status != 'sending' AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS telegram_outbox_sender_ready
ON telegram_outbox(sender_agent_id, status, available_at, created_at);
CREATE INDEX IF NOT EXISTS telegram_outbox_stale_lease
ON telegram_outbox(status, lease_expires_at);
"""


MIGRATION_11 = """
CREATE INDEX IF NOT EXISTS external_turn_excerpts_topic_turn
ON external_turn_excerpts(topic_id, turn_id);
CREATE TRIGGER IF NOT EXISTS provider_jobs_context_watermark_topic
BEFORE INSERT ON provider_jobs
WHEN NEW.context_watermark IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM external_turn_excerpts
     WHERE turn_id = NEW.context_watermark AND topic_id = NEW.topic_id
 )
BEGIN
    SELECT RAISE(ABORT, 'provider job context watermark is not a visible turn for topic');
END;
CREATE TRIGGER IF NOT EXISTS provider_jobs_context_watermark_topic_update
BEFORE UPDATE OF context_watermark ON provider_jobs
WHEN NEW.context_watermark IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM external_turn_excerpts
     WHERE turn_id = NEW.context_watermark AND topic_id = NEW.topic_id
 )
BEGIN
    SELECT RAISE(ABORT, 'provider job context watermark is not a visible turn for topic');
END;
UPDATE provider_jobs
SET context_watermark = NULL
WHERE context_watermark IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM external_turn_excerpts
      WHERE turn_id = provider_jobs.context_watermark
        AND topic_id = provider_jobs.topic_id
        AND created_at <= provider_jobs.created_at
  );
"""


MIGRATION_12 = """
CREATE TABLE IF NOT EXISTS runtime_health (
    component TEXT NOT NULL CHECK(component IN (
        'controller', 'sender', 'provider_worker'
    )),
    instance_id TEXT NOT NULL CHECK(length(instance_id) BETWEEN 1 AND 128),
    runtime TEXT CHECK(runtime IS NULL OR length(runtime) BETWEEN 1 AND 64),
    agent_id TEXT CHECK(agent_id IS NULL OR length(agent_id) BETWEEN 1 AND 64),
    pid INTEGER NOT NULL CHECK(pid > 0),
    process_start_marker TEXT NOT NULL
        CHECK(length(process_start_marker) BETWEEN 1 AND 128),
    started_at TEXT NOT NULL CHECK(length(started_at) BETWEEN 1 AND 64),
    heartbeat_at TEXT NOT NULL CHECK(length(heartbeat_at) BETWEEN 1 AND 64),
    success_at TEXT CHECK(success_at IS NULL OR length(success_at) BETWEEN 1 AND 64),
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 128),
    activity_state TEXT NOT NULL DEFAULT 'idle' CHECK(activity_state IN (
        'idle', 'leased', 'executing', 'sending', 'unknown'
    )),
    active_job_id TEXT CHECK(active_job_id IS NULL OR length(active_job_id) BETWEEN 1 AND 128),
    active_lease_expires_at TEXT CHECK(
        active_lease_expires_at IS NULL OR length(active_lease_expires_at) BETWEEN 1 AND 64
    ),
    provider_state TEXT NOT NULL DEFAULT 'unknown' CHECK(provider_state IN (
        'unknown', 'ready', 'limited', 'exhausted', 'unavailable'
    )),
    quota_remaining_percent REAL CHECK(
        quota_remaining_percent IS NULL
        OR (quota_remaining_percent >= 0 AND quota_remaining_percent <= 100)
    ),
    quota_reset_at TEXT CHECK(quota_reset_at IS NULL OR length(quota_reset_at) BETWEEN 1 AND 64),
    updated_at TEXT NOT NULL CHECK(length(updated_at) BETWEEN 1 AND 64),
    PRIMARY KEY(component, instance_id),
    CHECK(active_job_id IS NOT NULL OR activity_state IN ('idle', 'unknown')),
    CHECK(active_job_id IS NOT NULL OR active_lease_expires_at IS NULL),
    CHECK(runtime IS NOT NULL OR provider_state = 'unknown'),
    CHECK(runtime IS NOT NULL OR quota_remaining_percent IS NULL),
    CHECK(runtime IS NOT NULL OR quota_reset_at IS NULL)
);
CREATE INDEX IF NOT EXISTS runtime_health_heartbeat
ON runtime_health(heartbeat_at);
CREATE INDEX IF NOT EXISTS runtime_health_agent
ON runtime_health(agent_id, heartbeat_at);
"""


MIGRATION_13 = """
CREATE TABLE IF NOT EXISTS provider_job_inputs (
    job_id TEXT NOT NULL REFERENCES provider_jobs(job_id),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    input_text TEXT NOT NULL CHECK(length(input_text) BETWEEN 1 AND 20000),
    received_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, message_id),
    UNIQUE(job_id, part_index)
);
INSERT OR IGNORE INTO provider_job_inputs (
    job_id, chat_id, message_id, part_index, input_text, received_at
)
SELECT job_id, chat_id, message_id, 1, payload_text, created_at
FROM provider_jobs;
CREATE INDEX IF NOT EXISTS provider_job_inputs_job
ON provider_job_inputs(job_id, part_index);
CREATE TABLE IF NOT EXISTS provider_stop_requests (
    request_id TEXT PRIMARY KEY CHECK(length(request_id) BETWEEN 1 AND 128),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    target_agent_id TEXT NOT NULL CHECK(length(target_agent_id) BETWEEN 1 AND 64),
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
    cancelled_queued_count INTEGER NOT NULL DEFAULT 0 CHECK(cancelled_queued_count >= 0),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS provider_stop_requests_pending
ON provider_stop_requests(topic_id, target_agent_id, status, created_at);
CREATE TABLE IF NOT EXISTS provider_job_absorptions (
    child_job_id TEXT PRIMARY KEY REFERENCES provider_jobs(job_id),
    parent_job_id TEXT NOT NULL REFERENCES provider_jobs(job_id),
    provider_turn_id TEXT NOT NULL CHECK(length(provider_turn_id) BETWEEN 1 AND 256),
    created_at TEXT NOT NULL,
    CHECK(child_job_id != parent_job_id)
);
CREATE INDEX IF NOT EXISTS provider_job_absorptions_parent
ON provider_job_absorptions(parent_job_id, created_at);
"""


# Version 13 reached one live deployment after provider_job_inputs was added but
# before the stop/absorption tables were appended to MIGRATION_13.  Migration 14
# intentionally repeats the idempotent CREATE statements so upgraded databases
# converge with clean installations.
MIGRATION_14 = """
CREATE TABLE IF NOT EXISTS provider_stop_requests (
    request_id TEXT PRIMARY KEY CHECK(length(request_id) BETWEEN 1 AND 128),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    target_agent_id TEXT NOT NULL CHECK(length(target_agent_id) BETWEEN 1 AND 64),
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
    cancelled_queued_count INTEGER NOT NULL DEFAULT 0 CHECK(cancelled_queued_count >= 0),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS provider_stop_requests_pending
ON provider_stop_requests(topic_id, target_agent_id, status, created_at);
CREATE TABLE IF NOT EXISTS provider_job_absorptions (
    child_job_id TEXT PRIMARY KEY REFERENCES provider_jobs(job_id),
    parent_job_id TEXT NOT NULL REFERENCES provider_jobs(job_id),
    provider_turn_id TEXT NOT NULL CHECK(length(provider_turn_id) BETWEEN 1 AND 256),
    created_at TEXT NOT NULL,
    CHECK(child_job_id != parent_job_id)
);
CREATE INDEX IF NOT EXISTS provider_job_absorptions_parent
ON provider_job_absorptions(parent_job_id, created_at);
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
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
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
    if previous < 7:
        connection.executescript(MIGRATION_7)
        connection.execute("PRAGMA user_version = 7")
    if previous < 8:
        connection.executescript(MIGRATION_8)
        connection.execute("PRAGMA user_version = 8")
    if previous < 9:
        connection.executescript(MIGRATION_9)
        connection.execute("PRAGMA user_version = 9")
    if previous < 10:
        connection.executescript(MIGRATION_10)
        connection.execute("PRAGMA user_version = 10")
    if previous < 11:
        connection.executescript(MIGRATION_11)
        connection.execute("PRAGMA user_version = 11")
    if previous < 12:
        connection.executescript(MIGRATION_12)
        connection.execute("PRAGMA user_version = 12")
    if previous < 13:
        connection.executescript(MIGRATION_13)
        connection.execute("PRAGMA user_version = 13")
    if previous < 14:
        connection.executescript(MIGRATION_14)
        connection.execute("PRAGMA user_version = 14")
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
