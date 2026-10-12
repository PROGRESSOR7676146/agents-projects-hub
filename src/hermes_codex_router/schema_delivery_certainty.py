"""Schema 43: rebuild delivery tables without disabling foreign keys."""

DELIVERY_CERTAINTY_SCHEMA = """
CREATE TABLE provider_recovery_notice_parts (
    job_id TEXT NOT NULL REFERENCES provider_recovery_notices(job_id),
    outbox_id TEXT NOT NULL,
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    telegram_html TEXT NOT NULL,
    telegram_message_id INTEGER CHECK(telegram_message_id IS NULL OR telegram_message_id > 0),
    delivered_at TEXT,
    part_type TEXT NOT NULL,
    file_path TEXT,
    file_name TEXT,
    file_size INTEGER,
    file_sha256 TEXT,
    receipt_validation_version INTEGER NOT NULL CHECK(receipt_validation_version IN (0,1)),
    PRIMARY KEY(job_id,part_index)
);
CREATE TABLE telegram_outbox_m43 (
    outbox_id TEXT PRIMARY KEY CHECK(length(outbox_id) BETWEEN 1 AND 128),
    job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    sender_agent_id TEXT NOT NULL CHECK(length(sender_agent_id) BETWEEN 1 AND 64),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    telegram_html TEXT NOT NULL CHECK(length(telegram_html) BETWEEN 1 AND 200000),
    status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'delivered', 'failed', 'unknown')),
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
    send_started_at TEXT,
    CHECK(
        (status = 'sending' AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status != 'sending' AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE TABLE telegram_outbox_parts_m43 (
    outbox_id TEXT NOT NULL REFERENCES telegram_outbox_m43(outbox_id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    telegram_html TEXT NOT NULL CHECK(length(telegram_html) BETWEEN 1 AND 4090),
    telegram_message_id INTEGER CHECK(telegram_message_id IS NULL OR telegram_message_id > 0),
    delivered_at TEXT,
    part_type TEXT NOT NULL DEFAULT 'text',
    file_path TEXT,
    file_name TEXT,
    file_size INTEGER,
    file_sha256 TEXT,
    receipt_validation_version INTEGER NOT NULL DEFAULT 0 CHECK(receipt_validation_version IN (0,1)),
    PRIMARY KEY(outbox_id, part_index)
);
INSERT INTO telegram_outbox_m43 (
    outbox_id, job_id, sender_agent_id, chat_id, thread_id, telegram_html,
    status, attempt_count, available_at, lease_owner, lease_token, lease_expires_at,
    telegram_message_id, error_code, created_at, updated_at, delivered_at, send_started_at
)
SELECT outbox_id, job_id, sender_agent_id, chat_id, thread_id, telegram_html,
    CASE WHEN status='sending' THEN 'unknown' ELSE status END,
    attempt_count, available_at, NULL, NULL, NULL, telegram_message_id,
    CASE WHEN status='sending' THEN 'legacy_send_attempt_unknown' ELSE error_code END,
    created_at, updated_at, delivered_at, NULL
FROM telegram_outbox;
INSERT INTO telegram_outbox_parts_m43 (
    outbox_id, part_index, telegram_html, telegram_message_id, delivered_at,
    part_type, file_path, file_name, file_size, file_sha256, receipt_validation_version
)
SELECT outbox_id, part_index, telegram_html, telegram_message_id, delivered_at,
    part_type, file_path, file_name, file_size, file_sha256, 0
FROM telegram_outbox_parts;
DROP TABLE telegram_outbox_parts;
DROP TABLE telegram_outbox;
ALTER TABLE telegram_outbox_m43 RENAME TO telegram_outbox;
ALTER TABLE telegram_outbox_parts_m43 RENAME TO telegram_outbox_parts;
CREATE INDEX telegram_outbox_sender_ready
ON telegram_outbox(sender_agent_id, status, available_at, created_at);
CREATE INDEX telegram_outbox_stale_lease
ON telegram_outbox(status, lease_expires_at);
CREATE TRIGGER telegram_outbox_parts_artifact_insert
BEFORE INSERT ON telegram_outbox_parts
WHEN NOT COALESCE((
    (NEW.part_type = 'text' AND NEW.file_path IS NULL AND NEW.file_name IS NULL
        AND NEW.file_size IS NULL AND NEW.file_sha256 IS NULL)
    OR
    (NEW.part_type = 'document' AND length(NEW.file_path) BETWEEN 1 AND 4096
        AND length(NEW.file_name) BETWEEN 1 AND 255
        AND NEW.file_size BETWEEN 1 AND 52428800
        AND length(NEW.file_sha256) = 64)
), 0)
BEGIN
    SELECT RAISE(ABORT, 'invalid telegram outbox part');
END;
CREATE TRIGGER telegram_outbox_parts_artifact_update
BEFORE UPDATE OF part_type, file_path, file_name, file_size, file_sha256
ON telegram_outbox_parts
WHEN NOT COALESCE((
    (NEW.part_type = 'text' AND NEW.file_path IS NULL AND NEW.file_name IS NULL
        AND NEW.file_size IS NULL AND NEW.file_sha256 IS NULL)
    OR
    (NEW.part_type = 'document' AND length(NEW.file_path) BETWEEN 1 AND 4096
        AND length(NEW.file_name) BETWEEN 1 AND 255
        AND NEW.file_size BETWEEN 1 AND 52428800
        AND length(NEW.file_sha256) = 64)
), 0)
BEGIN
    SELECT RAISE(ABORT, 'invalid telegram outbox part');
END;
CREATE TABLE provider_progress_deliveries_m43 (
    progress_id TEXT PRIMARY KEY,
    item_sequence INTEGER NOT NULL UNIQUE REFERENCES provider_visible_items(sequence),
    job_id TEXT NOT NULL REFERENCES provider_jobs(job_id),
    sender_agent_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    telegram_html TEXT NOT NULL CHECK(length(telegram_html) BETWEEN 1 AND 4096),
    status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'delivered', 'superseded', 'failed', 'unknown')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    telegram_message_id INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    send_started_at TEXT
);
INSERT INTO provider_progress_deliveries_m43 (
    progress_id, item_sequence, job_id, sender_agent_id, chat_id, thread_id,
    telegram_html, status, attempt_count, available_at, lease_owner, lease_token,
    lease_expires_at, telegram_message_id, error_code, created_at, updated_at,
    delivered_at, send_started_at
)
SELECT progress_id, item_sequence, job_id, sender_agent_id, chat_id, thread_id,
    telegram_html, CASE WHEN status='sending' THEN 'unknown' ELSE status END,
    attempt_count, available_at, NULL, NULL, NULL, telegram_message_id,
    CASE WHEN status='sending' THEN 'legacy_send_attempt_unknown' ELSE error_code END,
    created_at, updated_at, delivered_at, NULL
FROM provider_progress_deliveries;
DROP TABLE provider_progress_deliveries;
ALTER TABLE provider_progress_deliveries_m43 RENAME TO provider_progress_deliveries;
CREATE INDEX provider_progress_delivery_ready
ON provider_progress_deliveries(sender_agent_id, status, available_at, created_at);
CREATE INDEX provider_progress_delivery_job
ON provider_progress_deliveries(job_id, created_at);
"""
