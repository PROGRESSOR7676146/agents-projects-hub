"""Schema 60: read-only topic status panel state and send evidence (ADR 0110).

``topic_panels`` holds the current panel of a topic; ``topic_panel_operations``
holds one immutable request per Telegram create/edit attempt and its outcome;
``topic_panel_pause`` keeps a bot-wide Telegram rate-limit pause for panels.
Panel rows are UI state only: they never authorize provider, queue, delivery or
stop effects. A settled operation never changes again, and an operation whose
outcome is unknown is never deleted, so a late receipt cannot rewrite evidence.
"""

from __future__ import annotations

TOPIC_PANEL_TABLES = ("topic_panel_pause", "topic_panel_operations", "topic_panels")
TOPIC_PANEL_TRIGGERS = (
    "topic_panel_operations_request_immutable",
    "topic_panel_operations_settled_immutable",
    "topic_panel_operations_retained",
)

TOPIC_PANEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_panels (
    topic_id INTEGER PRIMARY KEY REFERENCES topics(topic_id),
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    instance INTEGER NOT NULL CHECK(instance >= 1),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    message_id INTEGER CHECK(message_id IS NULL OR message_id > 0),
    confirmed_digest TEXT CHECK(confirmed_digest IS NULL OR length(confirmed_digest)=64),
    confirmed_at TEXT,
    -- The operation whose payload is the last confirmed version; never pruned.
    confirmed_operation_id TEXT,
    next_due_at TEXT NOT NULL,
    refresh_requested_at TEXT,
    unavailable_reason TEXT CHECK(
        unavailable_reason IS NULL OR length(unavailable_reason) BETWEEN 1 AND 64
    ),
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id,thread_id)
);
CREATE INDEX IF NOT EXISTS topic_panels_due
    ON topic_panels(next_due_at) WHERE enabled=1 AND unavailable_reason IS NULL;
CREATE TABLE IF NOT EXISTS topic_panel_operations (
    operation_id TEXT NOT NULL PRIMARY KEY CHECK(length(operation_id)=32),
    topic_id INTEGER NOT NULL REFERENCES topic_panels(topic_id),
    instance INTEGER NOT NULL CHECK(instance >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('create','edit')),
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    target_message_id INTEGER,
    payload_html TEXT NOT NULL,
    reply_markup TEXT NOT NULL,
    body_digest TEXT NOT NULL CHECK(length(body_digest)=64),
    state TEXT NOT NULL CHECK(state IN ('sending','confirmed','rejected','unknown')),
    lease_token TEXT NOT NULL CHECK(length(lease_token)=32),
    lease_expires_at TEXT NOT NULL,
    send_started_at TEXT NOT NULL,
    receipt_message_id INTEGER CHECK(receipt_message_id IS NULL OR receipt_message_id > 0),
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    completed_at TEXT,
    CHECK(
        (kind='create' AND target_message_id IS NULL)
        OR (kind='edit' AND target_message_id IS NOT NULL AND target_message_id > 0)
    ),
    CHECK((state='sending') = (completed_at IS NULL)),
    CHECK(state<>'confirmed' OR receipt_message_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS topic_panel_operations_open
    ON topic_panel_operations(topic_id) WHERE state='sending';
CREATE INDEX IF NOT EXISTS topic_panel_operations_history
    ON topic_panel_operations(topic_id,send_started_at);
CREATE TABLE IF NOT EXISTS topic_panel_pause (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    paused_until TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS topic_panel_operations_request_immutable
BEFORE UPDATE OF operation_id,topic_id,instance,kind,chat_id,thread_id,target_message_id,
    payload_html,reply_markup,body_digest,lease_token,lease_expires_at,send_started_at
ON topic_panel_operations
BEGIN
    SELECT RAISE(ABORT, 'topic panel operation request is immutable');
END;
CREATE TRIGGER IF NOT EXISTS topic_panel_operations_settled_immutable
BEFORE UPDATE ON topic_panel_operations
WHEN OLD.state<>'sending'
BEGIN
    SELECT RAISE(ABORT, 'settled topic panel operation is immutable');
END;
CREATE TRIGGER IF NOT EXISTS topic_panel_operations_retained
BEFORE DELETE ON topic_panel_operations
WHEN OLD.state IN ('sending','unknown')
BEGIN
    SELECT RAISE(ABORT, 'unsettled topic panel operation evidence is retained');
END;
"""
