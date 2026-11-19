"""Schema 59: immutable, owner-attested loss records for absent spool artifacts.

This is deliberately not delivery evidence.  It neither changes the outbox nor
authorizes a sender, replay, cleanup, or provider action.
"""

from __future__ import annotations

import sqlite3

ARTIFACT_LOSS_TABLES = frozenset(("artifact_loss_operations", "artifact_loss_items"))
ARTIFACT_LOSS_OPERATION_COLUMNS = frozenset(
    (
        "operation_id",
        "sequence",
        "previous_commit_hash",
        "snapshot_hash",
        "recovery_audit_sha256",
        "reason",
        "authority",
        "applied_at",
        "commit_hash",
    )
)
ARTIFACT_LOSS_ITEM_COLUMNS = frozenset(
    (
        "operation_id",
        "outbox_id",
        "part_index",
        "source_binding_hash",
        "file_sha256",
        "file_size",
    )
)
ARTIFACT_LOSS_TRIGGER_NAMES = frozenset(
    (
        "artifact_loss_operations_append_only",
        "artifact_loss_operations_no_update",
        "artifact_loss_operations_no_delete",
        "artifact_loss_items_insert_guard",
        "artifact_loss_items_no_update",
        "artifact_loss_items_no_delete",
        "artifact_loss_source_part_no_replace",
        "artifact_loss_source_part_identity_immutable",
        "artifact_loss_source_outbox_no_replace",
        "artifact_loss_source_outbox_binding_immutable",
        "artifact_loss_source_job_no_replace",
        "artifact_loss_source_job_binding_immutable",
        "artifact_loss_source_topic_no_replace",
        "artifact_loss_source_topic_binding_immutable",
        "artifact_loss_source_part_no_delete",
        "artifact_loss_source_outbox_no_delete",
        "artifact_loss_source_job_no_delete",
        "artifact_loss_source_topic_no_delete",
    )
)

ARTIFACT_LOSS_SCHEMA = """
CREATE TABLE artifact_loss_operations (
    operation_id TEXT NOT NULL PRIMARY KEY CHECK(length(operation_id)=36),
    sequence INTEGER NOT NULL UNIQUE CHECK(sequence > 0),
    previous_commit_hash TEXT NOT NULL
        CHECK(length(previous_commit_hash)=64 AND previous_commit_hash NOT GLOB '*[^0-9a-f]*'),
    snapshot_hash TEXT NOT NULL
        CHECK(length(snapshot_hash)=64 AND snapshot_hash NOT GLOB '*[^0-9a-f]*'),
    recovery_audit_sha256 TEXT NOT NULL
        CHECK(length(recovery_audit_sha256)=64 AND recovery_audit_sha256 NOT GLOB '*[^0-9a-f]*'),
    reason TEXT NOT NULL CHECK(reason='irrecoverable_after_search'),
    authority TEXT NOT NULL CHECK(authority='local_owner_cli'),
    applied_at TEXT NOT NULL,
    commit_hash TEXT NOT NULL UNIQUE
        CHECK(length(commit_hash)=64 AND commit_hash NOT GLOB '*[^0-9a-f]*')
) WITHOUT ROWID;

CREATE TRIGGER artifact_loss_operations_append_only
BEFORE INSERT ON artifact_loss_operations
WHEN NEW.sequence != COALESCE((
        SELECT sequence + 1 FROM artifact_loss_operations ORDER BY sequence DESC LIMIT 1
    ), 1)
 OR NEW.previous_commit_hash IS NOT COALESCE((
        SELECT commit_hash FROM artifact_loss_operations ORDER BY sequence DESC LIMIT 1
    ), '0000000000000000000000000000000000000000000000000000000000000000')
BEGIN SELECT RAISE(ABORT, 'artifact loss operation order is invalid'); END;

CREATE TRIGGER artifact_loss_operations_no_update
BEFORE UPDATE ON artifact_loss_operations
BEGIN SELECT RAISE(ABORT, 'artifact loss operation is immutable'); END;

CREATE TRIGGER artifact_loss_operations_no_delete
BEFORE DELETE ON artifact_loss_operations
BEGIN SELECT RAISE(ABORT, 'artifact loss operation cannot be deleted'); END;

CREATE TABLE artifact_loss_items (
    operation_id TEXT NOT NULL REFERENCES artifact_loss_operations(operation_id),
    outbox_id TEXT NOT NULL CHECK(length(outbox_id) BETWEEN 1 AND 128),
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    source_binding_hash TEXT NOT NULL
        CHECK(length(source_binding_hash)=64 AND source_binding_hash NOT GLOB '*[^0-9a-f]*'),
    file_sha256 TEXT NOT NULL
        CHECK(length(file_sha256)=64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'),
    file_size INTEGER NOT NULL CHECK(file_size BETWEEN 1 AND 52428800),
    PRIMARY KEY(operation_id, outbox_id, part_index),
    UNIQUE(outbox_id, part_index)
) WITHOUT ROWID;

CREATE TRIGGER artifact_loss_items_insert_guard
BEFORE INSERT ON artifact_loss_items
WHEN NOT EXISTS (
        SELECT 1 FROM artifact_loss_operations operation
        WHERE operation.operation_id=NEW.operation_id
    )
 OR NOT EXISTS (
        SELECT 1
        FROM telegram_outbox outbox
        JOIN telegram_outbox_parts part USING(outbox_id)
        WHERE outbox.outbox_id=NEW.outbox_id
          AND part.part_index=NEW.part_index
          AND part.part_type='document'
          AND part.file_sha256=NEW.file_sha256
          AND part.file_size=NEW.file_size
          AND outbox.status IN ('delivered','unknown','failed')
          AND outbox.lease_owner IS NULL AND outbox.lease_token IS NULL
          AND outbox.lease_expires_at IS NULL
          AND (outbox.status!='failed' OR outbox.attempt_count=20)
          AND NOT EXISTS (
              SELECT 1 FROM telegram_artifact_retirements retirement
              WHERE retirement.outbox_id=part.outbox_id AND retirement.part_index=part.part_index
          )
    )
BEGIN SELECT RAISE(ABORT, 'artifact loss item does not match an eligible source'); END;

CREATE TRIGGER artifact_loss_items_no_update
BEFORE UPDATE ON artifact_loss_items
BEGIN SELECT RAISE(ABORT, 'artifact loss item is immutable'); END;

CREATE TRIGGER artifact_loss_items_no_delete
BEFORE DELETE ON artifact_loss_items
BEGIN SELECT RAISE(ABORT, 'artifact loss item cannot be deleted'); END;

-- A late receipt may still settle an unknown send.  Freeze every source field
-- used by DeliveryReceipts._binding(), but not receipt/projection fields.
CREATE TRIGGER artifact_loss_source_part_no_replace
BEFORE INSERT ON telegram_outbox_parts
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_items item
    WHERE item.outbox_id=NEW.outbox_id AND item.part_index=NEW.part_index
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact part cannot be replaced'); END;

CREATE TRIGGER artifact_loss_source_part_identity_immutable
BEFORE UPDATE OF outbox_id,part_index,telegram_html,part_type,file_path,file_name,file_size,file_sha256
ON telegram_outbox_parts
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_items item
    WHERE item.outbox_id=OLD.outbox_id AND item.part_index=OLD.part_index
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
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact identity is immutable'); END;

CREATE TRIGGER artifact_loss_source_outbox_no_replace
BEFORE INSERT ON telegram_outbox
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_items item WHERE item.outbox_id=NEW.outbox_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact outbox cannot be replaced'); END;

CREATE TRIGGER artifact_loss_source_outbox_binding_immutable
BEFORE UPDATE OF outbox_id,job_id,sender_agent_id,chat_id,thread_id,purpose,telegram_html
ON telegram_outbox
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_items item WHERE item.outbox_id=OLD.outbox_id
)
AND (
    NEW.outbox_id IS NOT OLD.outbox_id
    OR NEW.job_id IS NOT OLD.job_id
    OR NEW.sender_agent_id IS NOT OLD.sender_agent_id
    OR NEW.chat_id IS NOT OLD.chat_id
    OR NEW.thread_id IS NOT OLD.thread_id
    OR NEW.purpose IS NOT OLD.purpose
    OR NEW.telegram_html IS NOT OLD.telegram_html
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact outbox binding is immutable'); END;

CREATE TRIGGER artifact_loss_source_job_no_replace
BEFORE INSERT ON provider_jobs
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item JOIN telegram_outbox outbox USING(outbox_id)
    WHERE outbox.job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact job cannot be replaced'); END;

CREATE TRIGGER artifact_loss_source_job_binding_immutable
BEFORE UPDATE OF job_id,session_id,session_generation,topic_id,topic_sequence,agent_id
ON provider_jobs
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item JOIN telegram_outbox outbox USING(outbox_id)
    WHERE outbox.job_id=OLD.job_id
)
AND (
    NEW.job_id IS NOT OLD.job_id
    OR NEW.session_id IS NOT OLD.session_id
    OR NEW.session_generation IS NOT OLD.session_generation
    OR NEW.topic_id IS NOT OLD.topic_id
    OR NEW.topic_sequence IS NOT OLD.topic_sequence
    OR NEW.agent_id IS NOT OLD.agent_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact job binding is immutable'); END;

CREATE TRIGGER artifact_loss_source_topic_no_replace
BEFORE INSERT ON topics
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item
    JOIN telegram_outbox outbox USING(outbox_id)
    JOIN provider_jobs job ON job.job_id=outbox.job_id
    WHERE job.topic_id=NEW.topic_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact topic cannot be replaced'); END;

CREATE TRIGGER artifact_loss_source_topic_binding_immutable
BEFORE UPDATE OF topic_id,project_id,chat_id,thread_id ON topics
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item
    JOIN telegram_outbox outbox USING(outbox_id)
    JOIN provider_jobs job ON job.job_id=outbox.job_id
    WHERE job.topic_id=OLD.topic_id
)
AND (
    NEW.topic_id IS NOT OLD.topic_id
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.chat_id IS NOT OLD.chat_id
    OR NEW.thread_id IS NOT OLD.thread_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact topic binding is immutable'); END;

CREATE TRIGGER artifact_loss_source_part_no_delete
BEFORE DELETE ON telegram_outbox_parts
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_items item
    WHERE item.outbox_id=OLD.outbox_id AND item.part_index=OLD.part_index
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact part cannot be deleted'); END;

CREATE TRIGGER artifact_loss_source_outbox_no_delete
BEFORE DELETE ON telegram_outbox
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_items item WHERE item.outbox_id=OLD.outbox_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact outbox cannot be deleted'); END;

CREATE TRIGGER artifact_loss_source_job_no_delete
BEFORE DELETE ON provider_jobs
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item JOIN telegram_outbox outbox USING(outbox_id)
    WHERE outbox.job_id=OLD.job_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact job cannot be deleted'); END;

CREATE TRIGGER artifact_loss_source_topic_no_delete
BEFORE DELETE ON topics
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item
    JOIN telegram_outbox outbox USING(outbox_id)
    JOIN provider_jobs job ON job.job_id=outbox.job_id
    WHERE job.topic_id=OLD.topic_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact topic cannot be deleted'); END;
"""


def has_complete_artifact_loss_schema(connection: sqlite3.Connection) -> bool:
    """Check the bounded structural prerequisites for schema-59 inventory."""
    expected_columns = {
        "artifact_loss_operations": ARTIFACT_LOSS_OPERATION_COLUMNS,
        "artifact_loss_items": ARTIFACT_LOSS_ITEM_COLUMNS,
    }
    for table, columns in expected_columns.items():
        object_type = connection.execute(
            "SELECT type FROM sqlite_master WHERE name=?", (table,)
        ).fetchone()
        if object_type is None or object_type[0] != "table":
            return False
        found = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if found != columns:
            return False
    foreign_keys = connection.execute("PRAGMA foreign_key_list(artifact_loss_items)").fetchall()
    if not any(
        row[2] == "artifact_loss_operations"
        and row[3] == "operation_id"
        and row[4] == "operation_id"
        for row in foreign_keys
    ):
        return False
    placeholders = ",".join("?" for _ in ARTIFACT_LOSS_TRIGGER_NAMES)
    triggers = {
        row[0]
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ({placeholders})",
            tuple(ARTIFACT_LOSS_TRIGGER_NAMES),
        )
    }
    return triggers == ARTIFACT_LOSS_TRIGGER_NAMES
