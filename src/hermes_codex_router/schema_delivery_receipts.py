"""Schema 54: immutable send attempts, receipt evidence and explicit new outbox purpose."""

DELIVERY_RECEIPTS_SCHEMA = """
ALTER TABLE telegram_outbox ADD COLUMN purpose TEXT NOT NULL DEFAULT 'legacy'
    CHECK(purpose IN ('legacy','provider_result','execution_notice'));
CREATE TRIGGER telegram_outbox_purpose_immutable
BEFORE UPDATE OF purpose ON telegram_outbox WHEN NEW.purpose != OLD.purpose
BEGIN SELECT RAISE(ABORT, 'outbox purpose is immutable'); END;
CREATE TRIGGER telegram_outbox_purpose_replace
BEFORE INSERT ON telegram_outbox
WHEN EXISTS (SELECT 1 FROM telegram_outbox WHERE outbox_id=NEW.outbox_id
             AND purpose != NEW.purpose)
BEGIN SELECT RAISE(ABORT, 'outbox purpose is immutable'); END;
CREATE TABLE telegram_send_attempts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE CHECK(length(attempt_id) BETWEEN 1 AND 128),
    target_kind TEXT NOT NULL CHECK(target_kind IN ('final','progress')),
    target_id TEXT NOT NULL,
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64),
    started_at TEXT NOT NULL
);
CREATE INDEX telegram_send_attempt_target
ON telegram_send_attempts(target_kind,target_id,part_index,sequence);
CREATE TABLE telegram_send_receipts (
    attempt_id TEXT PRIMARY KEY REFERENCES telegram_send_attempts(attempt_id),
    telegram_message_id INTEGER NOT NULL CHECK(typeof(telegram_message_id)='integer'
                                               AND telegram_message_id > 0),
    recorded_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER telegram_send_attempt_update BEFORE UPDATE ON telegram_send_attempts
BEGIN SELECT RAISE(ABORT, 'send attempt is immutable'); END;
CREATE TRIGGER telegram_send_attempt_delete BEFORE DELETE ON telegram_send_attempts
BEGIN SELECT RAISE(ABORT, 'send attempt is immutable'); END;
CREATE TRIGGER telegram_send_attempt_replace BEFORE INSERT ON telegram_send_attempts
WHEN EXISTS (SELECT 1 FROM telegram_send_attempts WHERE attempt_id=NEW.attempt_id
             OR sequence=NEW.sequence)
BEGIN SELECT RAISE(ABORT, 'send attempt is immutable'); END;
CREATE TRIGGER telegram_send_receipt_update BEFORE UPDATE ON telegram_send_receipts
BEGIN SELECT RAISE(ABORT, 'send receipt is immutable'); END;
CREATE TRIGGER telegram_send_receipt_delete BEFORE DELETE ON telegram_send_receipts
BEGIN SELECT RAISE(ABORT, 'send receipt is immutable'); END;
CREATE TRIGGER telegram_send_receipt_replace BEFORE INSERT ON telegram_send_receipts
WHEN EXISTS (SELECT 1 FROM telegram_send_receipts WHERE attempt_id=NEW.attempt_id)
BEGIN SELECT RAISE(ABORT, 'send receipt is immutable'); END;
"""
