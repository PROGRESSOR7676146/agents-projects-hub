"""Schema 61: agent mail between configured topics (ADR 0112).

``agent_mail_routes`` is the Controller's projection of the configured
directed routes, so workers that read another configuration copy offer and
check the same addresses. ``agent_letters`` holds one immutable letter per
source job, committed in that job's result transaction. ``agent_letter_deliveries``
holds the Hub-bot display in the target topic and, when that display fails or
stays unknown, one failure notice in the source topic. A letter never starts a
turn: only the owner's «Обработать» press admits ordinary input.
"""

from __future__ import annotations

AGENT_LETTER_TABLES = ("agent_letter_deliveries", "agent_letters", "agent_mail_routes")
AGENT_LETTER_TRIGGERS = (
    "agent_letters_immutable",
    "agent_letters_retained",
    "agent_letter_deliveries_request_immutable",
    "agent_letter_deliveries_settled_immutable",
    "agent_letter_deliveries_retained",
)

AGENT_LETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_mail_routes (
    source_chat_id INTEGER NOT NULL CHECK(source_chat_id != 0),
    source_thread_id INTEGER NOT NULL CHECK(source_thread_id > 0),
    alias TEXT NOT NULL CHECK(length(alias) BETWEEN 1 AND 16),
    source_project_id TEXT NOT NULL CHECK(length(source_project_id) BETWEEN 1 AND 128),
    target_project_id TEXT NOT NULL CHECK(length(target_project_id) BETWEEN 1 AND 128),
    target_chat_id INTEGER NOT NULL CHECK(target_chat_id != 0),
    target_thread_id INTEGER NOT NULL CHECK(target_thread_id > 0),
    PRIMARY KEY(source_chat_id, source_thread_id, alias),
    CHECK(source_chat_id != target_chat_id OR source_thread_id != target_thread_id)
);
CREATE TABLE IF NOT EXISTS agent_letters (
    letter_id TEXT PRIMARY KEY CHECK(length(letter_id)=32),
    source_job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    source_topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    source_agent_id TEXT NOT NULL CHECK(length(source_agent_id) BETWEEN 1 AND 64),
    route_alias TEXT NOT NULL CHECK(length(route_alias) BETWEEN 1 AND 16),
    -- The exact route at commit: displays, notices and presses recheck it.
    source_project_id TEXT NOT NULL CHECK(length(source_project_id) BETWEEN 1 AND 128),
    source_chat_id INTEGER NOT NULL CHECK(source_chat_id != 0),
    source_thread_id INTEGER NOT NULL CHECK(source_thread_id > 0),
    target_project_id TEXT NOT NULL CHECK(length(target_project_id) BETWEEN 1 AND 128),
    target_chat_id INTEGER NOT NULL CHECK(target_chat_id != 0),
    target_thread_id INTEGER NOT NULL CHECK(target_thread_id > 0),
    target_topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    file_name TEXT NOT NULL CHECK(length(file_name) BETWEEN 1 AND 128),
    body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 65536),
    body_sha256 TEXT NOT NULL CHECK(length(body_sha256)=64),
    caption_html TEXT NOT NULL CHECK(length(caption_html) BETWEEN 1 AND 1024),
    source_label TEXT NOT NULL CHECK(length(source_label) BETWEEN 1 AND 300),
    created_at TEXT NOT NULL,
    CHECK(source_topic_id != target_topic_id)
);
CREATE TABLE IF NOT EXISTS agent_letter_deliveries (
    letter_id TEXT NOT NULL REFERENCES agent_letters(letter_id),
    kind TEXT NOT NULL CHECK(kind IN ('display','failure_notice')),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    state TEXT NOT NULL CHECK(state IN ('pending','sending','confirmed','rejected','unknown')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
    available_at TEXT NOT NULL,
    lease_token TEXT CHECK(lease_token IS NULL OR length(lease_token)=32),
    lease_expires_at TEXT,
    send_started_at TEXT,
    receipt_message_id INTEGER CHECK(receipt_message_id IS NULL OR receipt_message_id > 0),
    receipt_file_id TEXT CHECK(
        receipt_file_id IS NULL OR length(receipt_file_id) BETWEEN 1 AND 512
    ),
    receipt_file_unique_id TEXT CHECK(
        receipt_file_unique_id IS NULL OR length(receipt_file_unique_id) BETWEEN 1 AND 512
    ),
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    completed_at TEXT,
    PRIMARY KEY(letter_id, kind),
    CHECK((state='sending') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK(state NOT IN ('sending','confirmed','unknown') OR send_started_at IS NOT NULL),
    CHECK(state<>'confirmed' OR receipt_message_id IS NOT NULL),
    -- A confirmed display must be processable: Telegram returned its document.
    CHECK(kind<>'display' OR state<>'confirmed'
        OR (receipt_file_id IS NOT NULL AND receipt_file_unique_id IS NOT NULL)),
    CHECK((state IN ('confirmed','rejected','unknown')) = (completed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS agent_letter_deliveries_due
    ON agent_letter_deliveries(state, available_at);
CREATE UNIQUE INDEX IF NOT EXISTS agent_letter_display_message
    ON agent_letter_deliveries(chat_id, receipt_message_id)
    WHERE kind='display' AND receipt_message_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS agent_letters_immutable
BEFORE UPDATE ON agent_letters
BEGIN
    SELECT RAISE(ABORT, 'agent letter is immutable');
END;
CREATE TRIGGER IF NOT EXISTS agent_letters_retained
BEFORE DELETE ON agent_letters
BEGIN
    SELECT RAISE(ABORT, 'agent letter is retained');
END;
CREATE TRIGGER IF NOT EXISTS agent_letter_deliveries_request_immutable
BEFORE UPDATE OF letter_id,kind,chat_id,thread_id ON agent_letter_deliveries
BEGIN
    SELECT RAISE(ABORT, 'agent letter delivery request is immutable');
END;
CREATE TRIGGER IF NOT EXISTS agent_letter_deliveries_settled_immutable
BEFORE UPDATE ON agent_letter_deliveries
WHEN OLD.state IN ('confirmed','rejected','unknown')
BEGIN
    SELECT RAISE(ABORT, 'settled agent letter delivery is immutable');
END;
CREATE TRIGGER IF NOT EXISTS agent_letter_deliveries_retained
BEFORE DELETE ON agent_letter_deliveries
BEGIN
    SELECT RAISE(ABORT, 'agent letter delivery evidence is retained');
END;
"""
