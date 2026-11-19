"""Schema 58: immutable receipt-bound retirement records for delivered documents."""

from __future__ import annotations

import sqlite3

from .delivery_receipts import DeliveryReceipts

ARTIFACT_RETIREMENT_TABLE = "telegram_artifact_retirements"
ARTIFACT_RETIREMENT_COLUMNS = frozenset(
    (
        "outbox_id",
        "part_index",
        "attempt_id",
        "binding_hash",
        "telegram_message_id",
        "delivered_at",
        "file_path",
        "file_name",
        "file_size",
        "file_sha256",
        "retired_at",
    )
)
ARTIFACT_RETIREMENT_TRIGGER_NAMES = frozenset(
    (
        "telegram_artifact_retirements_no_replace",
        "telegram_artifact_retirements_insert_guard",
        "telegram_artifact_retirements_no_update",
        "telegram_artifact_retirements_no_delete",
        "telegram_artifact_retirement_source_part_no_replace",
        "telegram_artifact_retirement_source_part_immutable",
        "telegram_artifact_retirement_source_outbox_no_replace",
        "telegram_artifact_retirement_source_outbox_immutable",
    )
)

ARTIFACT_RETIREMENT_SCHEMA = """
CREATE TABLE telegram_artifact_retirements (
    outbox_id TEXT NOT NULL CHECK(length(outbox_id) BETWEEN 1 AND 128),
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES telegram_send_attempts(attempt_id)
        CHECK(length(attempt_id) BETWEEN 1 AND 128),
    binding_hash TEXT NOT NULL
        CHECK(length(binding_hash)=64 AND binding_hash NOT GLOB '*[^0-9a-f]*'),
    telegram_message_id INTEGER NOT NULL
        CHECK(typeof(telegram_message_id)='integer' AND telegram_message_id > 0),
    delivered_at TEXT NOT NULL,
    file_path TEXT NOT NULL CHECK(length(file_path) BETWEEN 1 AND 4096),
    file_name TEXT NOT NULL CHECK(length(file_name) BETWEEN 1 AND 255),
    file_size INTEGER NOT NULL CHECK(file_size BETWEEN 1 AND 52428800),
    file_sha256 TEXT NOT NULL
        CHECK(length(file_sha256)=64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'),
    retired_at TEXT NOT NULL,
    PRIMARY KEY(outbox_id,part_index)
) WITHOUT ROWID;

-- The live outbox can be atomically archived after terminal reconciliation, so
-- this record intentionally has no foreign key to it.  The immutable attempt
-- ledger remains the durable parent of the exact transport proof.
CREATE TRIGGER telegram_artifact_retirements_no_replace
BEFORE INSERT ON telegram_artifact_retirements
WHEN EXISTS (
    SELECT 1 FROM telegram_artifact_retirements current
    WHERE current.outbox_id=NEW.outbox_id AND current.part_index=NEW.part_index
)
OR EXISTS (
    SELECT 1 FROM telegram_artifact_retirements current
    WHERE current.attempt_id=NEW.attempt_id
)
BEGIN SELECT RAISE(ABORT, 'artifact retirement is immutable'); END;

CREATE TRIGGER telegram_artifact_retirements_insert_guard
BEFORE INSERT ON telegram_artifact_retirements
WHEN NOT EXISTS (
    SELECT 1
    FROM telegram_outbox outbox
    JOIN telegram_outbox_parts part
      ON part.outbox_id=outbox.outbox_id
    JOIN telegram_send_attempts attempt
      ON attempt.attempt_id=NEW.attempt_id
    JOIN telegram_send_receipts receipt
      ON receipt.attempt_id=attempt.attempt_id
    WHERE outbox.outbox_id=NEW.outbox_id
      AND outbox.status='delivered'
      AND outbox.delivered_at IS NOT NULL
      AND outbox.delivered_at IS NEW.retired_at
      AND part.outbox_id=NEW.outbox_id
      AND part.part_index=NEW.part_index
      AND part.part_type='document'
      AND part.receipt_validation_version=1
      AND part.telegram_message_id IS NEW.telegram_message_id
      AND part.delivered_at IS NEW.delivered_at
      AND part.file_path IS NEW.file_path
      AND part.file_name IS NEW.file_name
      AND part.file_size IS NEW.file_size
      AND part.file_sha256 IS NEW.file_sha256
      AND attempt.target_kind='final'
      AND attempt.target_id=NEW.outbox_id
      AND attempt.part_index=NEW.part_index
      AND attempt.binding_hash=NEW.binding_hash
      AND receipt.telegram_message_id IS NEW.telegram_message_id
      AND receipt.recorded_at IS NEW.delivered_at
      AND NOT EXISTS (
          SELECT 1 FROM telegram_send_attempts newer
          WHERE newer.target_kind='final'
            AND newer.target_id=attempt.target_id
            AND newer.part_index=attempt.part_index
            AND newer.sequence>attempt.sequence
      )
      AND NOT EXISTS (
          SELECT 1 FROM telegram_outbox_parts all_part
          WHERE all_part.outbox_id=NEW.outbox_id
            AND (
                all_part.receipt_validation_version!=1
                OR all_part.telegram_message_id IS NULL
                OR all_part.delivered_at IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM telegram_send_attempts all_attempt
                    JOIN telegram_send_receipts all_receipt
                      ON all_receipt.attempt_id=all_attempt.attempt_id
                    WHERE all_attempt.target_kind='final'
                      AND all_attempt.target_id=NEW.outbox_id
                      AND all_attempt.part_index=all_part.part_index
                      AND all_receipt.telegram_message_id=all_part.telegram_message_id
                      AND all_receipt.recorded_at=all_part.delivered_at
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_send_attempts newer
                          WHERE newer.target_kind='final'
                            AND newer.target_id=all_attempt.target_id
                            AND newer.part_index=all_attempt.part_index
                            AND newer.sequence>all_attempt.sequence
                      )
                )
            )
      )
)
BEGIN SELECT RAISE(ABORT, 'artifact retirement lacks exact receipt evidence'); END;

CREATE TRIGGER telegram_artifact_retirements_no_update
BEFORE UPDATE ON telegram_artifact_retirements
BEGIN SELECT RAISE(ABORT, 'artifact retirement is immutable'); END;

CREATE TRIGGER telegram_artifact_retirements_no_delete
BEFORE DELETE ON telegram_artifact_retirements
BEGIN SELECT RAISE(ABORT, 'artifact retirement cannot be deleted'); END;

CREATE TRIGGER telegram_artifact_retirement_source_part_no_replace
BEFORE INSERT ON telegram_outbox_parts
WHEN EXISTS (
    SELECT 1 FROM telegram_artifact_retirements retirement
    WHERE retirement.outbox_id=NEW.outbox_id AND retirement.part_index=NEW.part_index
)
BEGIN SELECT RAISE(ABORT, 'retired artifact part cannot be replaced'); END;

CREATE TRIGGER telegram_artifact_retirement_source_part_immutable
BEFORE UPDATE OF outbox_id,part_index,telegram_html,part_type,file_path,file_name,file_size,
                 file_sha256,telegram_message_id,delivered_at,receipt_validation_version
ON telegram_outbox_parts
WHEN EXISTS (
    SELECT 1 FROM telegram_artifact_retirements retirement
    WHERE retirement.outbox_id=OLD.outbox_id AND retirement.part_index=OLD.part_index
)
AND (
    NEW.outbox_id IS NOT OLD.outbox_id
    OR NEW.part_index IS NOT OLD.part_index
    OR NEW.telegram_html IS NOT OLD.telegram_html
    OR NEW.part_type IS NOT OLD.part_type
    OR NEW.file_path IS NOT OLD.file_path
    OR NEW.file_name IS NOT OLD.file_name
    OR NEW.file_size IS NOT OLD.file_size
    OR NEW.file_sha256 IS NOT OLD.file_sha256
    OR NEW.telegram_message_id IS NOT OLD.telegram_message_id
    OR NEW.delivered_at IS NOT OLD.delivered_at
    OR NEW.receipt_validation_version IS NOT OLD.receipt_validation_version
)
BEGIN SELECT RAISE(ABORT, 'retired artifact part is immutable'); END;

CREATE TRIGGER telegram_artifact_retirement_source_outbox_no_replace
BEFORE INSERT ON telegram_outbox
WHEN EXISTS (
    SELECT 1 FROM telegram_artifact_retirements retirement
    WHERE retirement.outbox_id=NEW.outbox_id
)
BEGIN SELECT RAISE(ABORT, 'retired artifact outbox cannot be replaced'); END;

CREATE TRIGGER telegram_artifact_retirement_source_outbox_immutable
BEFORE UPDATE OF outbox_id,job_id,sender_agent_id,chat_id,thread_id,telegram_html,status,
                 telegram_message_id,delivered_at,purpose
ON telegram_outbox
WHEN EXISTS (
    SELECT 1 FROM telegram_artifact_retirements retirement
    WHERE retirement.outbox_id=OLD.outbox_id
)
AND (
    NEW.outbox_id IS NOT OLD.outbox_id
    OR NEW.job_id IS NOT OLD.job_id
    OR NEW.sender_agent_id IS NOT OLD.sender_agent_id
    OR NEW.chat_id IS NOT OLD.chat_id
    OR NEW.thread_id IS NOT OLD.thread_id
    OR NEW.telegram_html IS NOT OLD.telegram_html
    OR NEW.status IS NOT OLD.status
    OR NEW.telegram_message_id IS NOT OLD.telegram_message_id
    OR NEW.delivered_at IS NOT OLD.delivered_at
    OR NEW.purpose IS NOT OLD.purpose
)
BEGIN SELECT RAISE(ABORT, 'retired artifact outbox is immutable'); END;
"""


def has_complete_artifact_retirement_schema(connection: sqlite3.Connection) -> bool:
    """Whether a schema-58 predecessor retains its receipt-proof guard set."""
    object_type = connection.execute(
        "SELECT type FROM sqlite_master WHERE name=?", (ARTIFACT_RETIREMENT_TABLE,)
    ).fetchone()
    if object_type is None or object_type[0] != "table":
        return False
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(telegram_artifact_retirements)")
    }
    if columns != ARTIFACT_RETIREMENT_COLUMNS:
        return False
    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?,?,?,?,?,?)",
            tuple(ARTIFACT_RETIREMENT_TRIGGER_NAMES),
        )
    }
    return triggers == ARTIFACT_RETIREMENT_TRIGGER_NAMES


def backfill_artifact_retirements(connection: sqlite3.Connection) -> None:
    """Retain only existing terminal v1 proof; missing history remains unretired."""
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        receipts = DeliveryReceipts(connection, RuntimeError)
        parts = connection.execute(
            """SELECT part.outbox_id,part.part_index
               FROM telegram_outbox outbox
               JOIN telegram_outbox_parts part USING(outbox_id)
               WHERE outbox.status='delivered' AND outbox.delivered_at IS NOT NULL
                 AND part.part_type='document' AND part.receipt_validation_version=1
               ORDER BY part.outbox_id,part.part_index"""
        ).fetchall()
        for part in parts:
            snapshot = receipts.artifact_retirement_snapshot(
                str(part["outbox_id"]), int(part["part_index"])
            )
            if snapshot is not None:
                receipts.insert_artifact_retirement(snapshot)
    finally:
        connection.row_factory = previous_factory
