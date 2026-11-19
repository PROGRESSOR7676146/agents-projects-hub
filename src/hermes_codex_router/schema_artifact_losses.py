"""Schema 59: immutable, owner-attested loss records for absent spool artifacts.

This is deliberately not delivery evidence.  It neither authorizes a sender,
replay, cleanup, or provider action, apart from preserving one exact normal
recovery-notice archive before its frozen source may be removed.
"""

from __future__ import annotations

import re
import sqlite3

ARTIFACT_LOSS_TABLES = frozenset(
    (
        "artifact_loss_operations",
        "artifact_loss_items",
        "artifact_loss_archive_tombstones",
    )
)
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
ARTIFACT_LOSS_ARCHIVE_TOMBSTONE_COLUMNS = frozenset(
    (
        "operation_id",
        "outbox_id",
        "part_index",
        "source_binding_hash",
        "file_sha256",
        "file_size",
        "source_job_id",
        "source_sender_agent_id",
        "source_chat_id",
        "source_thread_id",
        "source_purpose",
        "source_telegram_html",
        "source_status",
        "source_attempt_count",
        "source_telegram_message_id",
        "source_delivered_at",
        "source_part_telegram_html",
        "source_part_type",
        "source_file_path",
        "source_file_name",
        "source_part_telegram_message_id",
        "source_part_delivered_at",
        "source_part_receipt_validation_version",
        "source_session_id",
        "source_session_generation",
        "source_topic_id",
        "source_topic_sequence",
        "source_agent_id",
        "source_project_id",
        "source_topic_chat_id",
        "source_topic_thread_id",
        "archive_job_id",
        "archive_outbox_id",
        "archive_part_index",
        "archive_saved_at",
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
        "artifact_loss_archive_tombstones_no_replace",
        "artifact_loss_archive_tombstones_insert_guard",
        "artifact_loss_archive_tombstones_no_update",
        "artifact_loss_archive_tombstones_no_delete",
        "artifact_loss_archive_notice_no_replace",
        "artifact_loss_archive_notice_immutable",
        "artifact_loss_archive_notice_no_delete",
        "artifact_loss_archive_part_no_replace",
        "artifact_loss_archive_part_immutable",
        "artifact_loss_archive_part_no_delete",
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

-- A declared-loss source may be archived by the normal recovery-notice path.
-- The tombstone records that the exact archived copy existed before removing the
-- live source; it is not an alternate way to declare loss or delivery success.
CREATE TABLE artifact_loss_archive_tombstones (
    operation_id TEXT NOT NULL REFERENCES artifact_loss_operations(operation_id)
        CHECK(length(operation_id)=36),
    outbox_id TEXT NOT NULL CHECK(length(outbox_id) BETWEEN 1 AND 128),
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    source_binding_hash TEXT NOT NULL
        CHECK(length(source_binding_hash)=64 AND source_binding_hash NOT GLOB '*[^0-9a-f]*'),
    file_sha256 TEXT NOT NULL
        CHECK(length(file_sha256)=64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'),
    file_size INTEGER NOT NULL CHECK(file_size BETWEEN 1 AND 52428800),
    source_job_id TEXT NOT NULL CHECK(length(source_job_id) BETWEEN 1 AND 128),
    source_sender_agent_id TEXT NOT NULL CHECK(length(source_sender_agent_id) BETWEEN 1 AND 64),
    source_chat_id INTEGER NOT NULL CHECK(source_chat_id != 0),
    source_thread_id INTEGER NOT NULL CHECK(source_thread_id > 0),
    source_purpose TEXT NOT NULL CHECK(length(source_purpose) BETWEEN 1 AND 64),
    source_telegram_html TEXT NOT NULL CHECK(length(source_telegram_html) BETWEEN 1 AND 200000),
    source_status TEXT NOT NULL CHECK(source_status IN ('delivered','unknown','failed')),
    source_attempt_count INTEGER NOT NULL CHECK(source_attempt_count BETWEEN 0 AND 20),
    source_telegram_message_id INTEGER
        CHECK(source_telegram_message_id IS NULL OR source_telegram_message_id > 0),
    source_delivered_at TEXT,
    source_part_telegram_html TEXT NOT NULL
        CHECK(length(source_part_telegram_html) BETWEEN 1 AND 4090),
    source_part_type TEXT NOT NULL CHECK(source_part_type='document'),
    source_file_path TEXT NOT NULL CHECK(length(source_file_path) BETWEEN 1 AND 4096),
    source_file_name TEXT NOT NULL CHECK(length(source_file_name) BETWEEN 1 AND 255),
    source_part_telegram_message_id INTEGER
        CHECK(source_part_telegram_message_id IS NULL OR source_part_telegram_message_id > 0),
    source_part_delivered_at TEXT,
    source_part_receipt_validation_version INTEGER NOT NULL
        CHECK(source_part_receipt_validation_version IN (0,1)),
    source_session_id TEXT NOT NULL,
    source_session_generation INTEGER NOT NULL,
    source_topic_id INTEGER NOT NULL,
    source_topic_sequence INTEGER NOT NULL,
    source_agent_id TEXT NOT NULL,
    source_project_id TEXT NOT NULL,
    source_topic_chat_id INTEGER NOT NULL CHECK(source_topic_chat_id != 0),
    source_topic_thread_id INTEGER NOT NULL CHECK(source_topic_thread_id > 0),
    archive_job_id TEXT NOT NULL CHECK(length(archive_job_id) BETWEEN 1 AND 128),
    archive_outbox_id TEXT NOT NULL CHECK(length(archive_outbox_id) BETWEEN 1 AND 128),
    archive_part_index INTEGER NOT NULL CHECK(archive_part_index > 0),
    archive_saved_at TEXT NOT NULL,
    PRIMARY KEY(outbox_id,part_index),
    FOREIGN KEY(operation_id,outbox_id,part_index)
        REFERENCES artifact_loss_items(operation_id,outbox_id,part_index),
    CHECK(archive_outbox_id=outbox_id),
    CHECK(archive_part_index=part_index)
) WITHOUT ROWID;

CREATE TRIGGER artifact_loss_archive_tombstones_no_replace
BEFORE INSERT ON artifact_loss_archive_tombstones
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones current
    WHERE current.outbox_id=NEW.outbox_id AND current.part_index=NEW.part_index
)
BEGIN SELECT RAISE(ABORT, 'artifact loss archive tombstone is immutable'); END;

CREATE TRIGGER artifact_loss_archive_tombstones_insert_guard
BEFORE INSERT ON artifact_loss_archive_tombstones
WHEN NOT EXISTS (
    SELECT 1
    FROM artifact_loss_items item
    JOIN telegram_outbox outbox ON outbox.outbox_id=item.outbox_id
    JOIN telegram_outbox_parts part
      ON part.outbox_id=outbox.outbox_id AND part.part_index=item.part_index
    JOIN provider_jobs job ON job.job_id=outbox.job_id
    JOIN topics topic ON topic.topic_id=job.topic_id
    JOIN provider_recovery_notices notice ON notice.job_id=outbox.job_id
    JOIN provider_recovery_notice_parts archived
      ON archived.job_id=notice.job_id AND archived.part_index=part.part_index
    WHERE item.operation_id=NEW.operation_id
      AND item.outbox_id=NEW.outbox_id
      AND item.part_index=NEW.part_index
      AND item.source_binding_hash=NEW.source_binding_hash
      AND item.file_sha256=NEW.file_sha256
      AND item.file_size=NEW.file_size
      AND NEW.source_job_id=outbox.job_id
      AND NEW.source_sender_agent_id=outbox.sender_agent_id
      AND NEW.source_chat_id=outbox.chat_id
      AND NEW.source_thread_id=outbox.thread_id
      AND NEW.source_purpose=outbox.purpose
      AND NEW.source_telegram_html IS outbox.telegram_html
      AND NEW.source_status=outbox.status
      AND NEW.source_attempt_count=outbox.attempt_count
      AND NEW.source_telegram_message_id IS outbox.telegram_message_id
      AND NEW.source_delivered_at IS outbox.delivered_at
      AND NEW.source_part_telegram_html IS part.telegram_html
      AND NEW.source_part_type=part.part_type
      AND NEW.source_file_path IS part.file_path
      AND NEW.source_file_name IS part.file_name
      AND NEW.source_part_telegram_message_id IS part.telegram_message_id
      AND NEW.source_part_delivered_at IS part.delivered_at
      AND NEW.source_part_receipt_validation_version=part.receipt_validation_version
      AND NEW.source_session_id=job.session_id
      AND NEW.source_session_generation=job.session_generation
      AND NEW.source_topic_id=job.topic_id
      AND NEW.source_topic_sequence=job.topic_sequence
      AND NEW.source_agent_id=job.agent_id
      AND NEW.source_project_id=topic.project_id
      AND NEW.source_topic_chat_id=topic.chat_id
      AND NEW.source_topic_thread_id=topic.thread_id
      AND NEW.archive_job_id=notice.job_id
      AND NEW.archive_outbox_id=notice.outbox_id
      AND NEW.archive_part_index=archived.part_index
      AND NEW.archive_saved_at=notice.saved_at
      AND part.part_type='document'
      AND part.file_sha256=NEW.file_sha256
      AND part.file_size=NEW.file_size
      AND outbox.status IN ('delivered','unknown','failed')
      AND outbox.lease_owner IS NULL AND outbox.lease_token IS NULL
      AND outbox.lease_expires_at IS NULL
      AND (outbox.status!='failed' OR outbox.attempt_count=20)
      AND notice.outbox_id=outbox.outbox_id
      AND notice.telegram_html IS outbox.telegram_html
      AND notice.delivery_status=outbox.status
      AND notice.telegram_message_id IS outbox.telegram_message_id
      AND archived.outbox_id=outbox.outbox_id
      AND archived.telegram_html IS part.telegram_html
      AND archived.telegram_message_id IS part.telegram_message_id
      AND archived.delivered_at IS part.delivered_at
      AND archived.part_type=part.part_type
      AND archived.file_path IS part.file_path
      AND archived.file_name IS part.file_name
      AND archived.file_size IS part.file_size
      AND archived.file_sha256 IS part.file_sha256
      AND archived.receipt_validation_version=part.receipt_validation_version
)
BEGIN SELECT RAISE(ABORT, 'artifact loss archive lacks an exact source copy'); END;

CREATE TRIGGER artifact_loss_archive_tombstones_no_update
BEFORE UPDATE ON artifact_loss_archive_tombstones
BEGIN SELECT RAISE(ABORT, 'artifact loss archive tombstone is immutable'); END;

CREATE TRIGGER artifact_loss_archive_tombstones_no_delete
BEFORE DELETE ON artifact_loss_archive_tombstones
BEGIN SELECT RAISE(ABORT, 'artifact loss archive tombstone cannot be deleted'); END;

CREATE TRIGGER artifact_loss_archive_notice_no_replace
BEFORE INSERT ON provider_recovery_notices
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.archive_job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss archive notice cannot be replaced'); END;

CREATE TRIGGER artifact_loss_archive_notice_immutable
BEFORE UPDATE OF job_id,outbox_id,telegram_html,delivery_status,telegram_message_id,saved_at
ON provider_recovery_notices
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.archive_job_id=OLD.job_id
)
AND (
    NEW.job_id IS NOT OLD.job_id
    OR NEW.outbox_id IS NOT OLD.outbox_id
    OR NEW.telegram_html IS NOT OLD.telegram_html
    OR NEW.delivery_status IS NOT OLD.delivery_status
    OR NEW.telegram_message_id IS NOT OLD.telegram_message_id
    OR NEW.saved_at IS NOT OLD.saved_at
)
BEGIN SELECT RAISE(ABORT, 'declared-loss archive notice is immutable'); END;

CREATE TRIGGER artifact_loss_archive_notice_no_delete
BEFORE DELETE ON provider_recovery_notices
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.archive_job_id=OLD.job_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss archive notice cannot be deleted'); END;

CREATE TRIGGER artifact_loss_archive_part_no_replace
BEFORE INSERT ON provider_recovery_notice_parts
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.archive_job_id=NEW.job_id
      AND tombstone.archive_part_index=NEW.part_index
)
BEGIN SELECT RAISE(ABORT, 'declared-loss archive part cannot be replaced'); END;

CREATE TRIGGER artifact_loss_archive_part_immutable
BEFORE UPDATE OF job_id,outbox_id,part_index,telegram_html,telegram_message_id,delivered_at,
                 part_type,file_path,file_name,file_size,file_sha256,receipt_validation_version
ON provider_recovery_notice_parts
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.archive_job_id=OLD.job_id
      AND tombstone.archive_part_index=OLD.part_index
)
AND (
    NEW.job_id IS NOT OLD.job_id
    OR NEW.outbox_id IS NOT OLD.outbox_id
    OR NEW.part_index IS NOT OLD.part_index
    OR NEW.telegram_html IS NOT OLD.telegram_html
    OR NEW.telegram_message_id IS NOT OLD.telegram_message_id
    OR NEW.delivered_at IS NOT OLD.delivered_at
    OR NEW.part_type IS NOT OLD.part_type
    OR NEW.file_path IS NOT OLD.file_path
    OR NEW.file_name IS NOT OLD.file_name
    OR NEW.file_size IS NOT OLD.file_size
    OR NEW.file_sha256 IS NOT OLD.file_sha256
    OR NEW.receipt_validation_version IS NOT OLD.receipt_validation_version
)
BEGIN SELECT RAISE(ABORT, 'declared-loss archive part is immutable'); END;

CREATE TRIGGER artifact_loss_archive_part_no_delete
BEFORE DELETE ON provider_recovery_notice_parts
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.archive_job_id=OLD.job_id
      AND tombstone.archive_part_index=OLD.part_index
)
BEGIN SELECT RAISE(ABORT, 'declared-loss archive part cannot be deleted'); END;

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
OR EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.source_job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact job cannot be replaced'); END;

CREATE TRIGGER artifact_loss_source_job_binding_immutable
BEFORE UPDATE OF job_id,session_id,session_generation,topic_id,topic_sequence,agent_id
ON provider_jobs
WHEN (
    EXISTS (
        SELECT 1
        FROM artifact_loss_items item JOIN telegram_outbox outbox USING(outbox_id)
        WHERE outbox.job_id=OLD.job_id
    )
    OR EXISTS (
        SELECT 1 FROM artifact_loss_archive_tombstones tombstone
        WHERE tombstone.source_job_id=OLD.job_id
    )
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
OR EXISTS (
    SELECT 1 FROM artifact_loss_archive_tombstones tombstone
    WHERE tombstone.source_topic_id=NEW.topic_id
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact topic cannot be replaced'); END;

CREATE TRIGGER artifact_loss_source_topic_binding_immutable
BEFORE UPDATE OF topic_id,project_id,chat_id,thread_id ON topics
WHEN (
    EXISTS (
        SELECT 1
        FROM artifact_loss_items item
        JOIN telegram_outbox outbox USING(outbox_id)
        JOIN provider_jobs job ON job.job_id=outbox.job_id
        WHERE job.topic_id=OLD.topic_id
    )
    OR EXISTS (
        SELECT 1 FROM artifact_loss_archive_tombstones tombstone
        WHERE tombstone.source_topic_id=OLD.topic_id
    )
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
    SELECT 1
    FROM artifact_loss_items item
    WHERE item.outbox_id=OLD.outbox_id
      AND item.part_index=OLD.part_index
      AND NOT EXISTS (
          SELECT 1
          FROM artifact_loss_archive_tombstones tombstone
          JOIN telegram_outbox outbox ON outbox.outbox_id=OLD.outbox_id
          JOIN provider_jobs job ON job.job_id=outbox.job_id
          JOIN topics topic ON topic.topic_id=job.topic_id
          JOIN provider_recovery_notices notice ON notice.job_id=tombstone.archive_job_id
          JOIN provider_recovery_notice_parts archived
            ON archived.job_id=tombstone.archive_job_id
           AND archived.part_index=tombstone.archive_part_index
          WHERE tombstone.operation_id=item.operation_id
            AND tombstone.outbox_id=item.outbox_id
            AND tombstone.part_index=item.part_index
            AND tombstone.source_binding_hash=item.source_binding_hash
            AND tombstone.file_sha256=item.file_sha256
            AND tombstone.file_size=item.file_size
            AND tombstone.source_job_id=outbox.job_id
            AND tombstone.source_sender_agent_id=outbox.sender_agent_id
            AND tombstone.source_chat_id=outbox.chat_id
            AND tombstone.source_thread_id=outbox.thread_id
            AND tombstone.source_purpose=outbox.purpose
            AND tombstone.source_telegram_html IS outbox.telegram_html
            AND tombstone.source_status=outbox.status
            AND tombstone.source_attempt_count=outbox.attempt_count
            AND tombstone.source_telegram_message_id IS outbox.telegram_message_id
            AND tombstone.source_delivered_at IS outbox.delivered_at
            AND tombstone.source_part_telegram_html IS OLD.telegram_html
            AND tombstone.source_part_type=OLD.part_type
            AND tombstone.source_file_path IS OLD.file_path
            AND tombstone.source_file_name IS OLD.file_name
            AND tombstone.file_size IS OLD.file_size
            AND tombstone.file_sha256 IS OLD.file_sha256
            AND tombstone.source_part_telegram_message_id IS OLD.telegram_message_id
            AND tombstone.source_part_delivered_at IS OLD.delivered_at
            AND tombstone.source_part_receipt_validation_version=OLD.receipt_validation_version
            AND tombstone.source_session_id=job.session_id
            AND tombstone.source_session_generation=job.session_generation
            AND tombstone.source_topic_id=job.topic_id
            AND tombstone.source_topic_sequence=job.topic_sequence
            AND tombstone.source_agent_id=job.agent_id
            AND tombstone.source_project_id=topic.project_id
            AND tombstone.source_topic_chat_id=topic.chat_id
            AND tombstone.source_topic_thread_id=topic.thread_id
            AND notice.outbox_id=tombstone.archive_outbox_id
            AND notice.telegram_html IS tombstone.source_telegram_html
            AND notice.delivery_status=tombstone.source_status
            AND notice.telegram_message_id IS tombstone.source_telegram_message_id
            AND notice.saved_at=tombstone.archive_saved_at
            AND archived.outbox_id=tombstone.archive_outbox_id
            AND archived.telegram_html IS tombstone.source_part_telegram_html
            AND archived.telegram_message_id IS tombstone.source_part_telegram_message_id
            AND archived.delivered_at IS tombstone.source_part_delivered_at
            AND archived.part_type=tombstone.source_part_type
            AND archived.file_path IS tombstone.source_file_path
            AND archived.file_name IS tombstone.source_file_name
            AND archived.file_size=tombstone.file_size
            AND archived.file_sha256=tombstone.file_sha256
            AND archived.receipt_validation_version=tombstone.source_part_receipt_validation_version
      )
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact part cannot be deleted'); END;

CREATE TRIGGER artifact_loss_source_outbox_no_delete
BEFORE DELETE ON telegram_outbox
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item
    WHERE item.outbox_id=OLD.outbox_id
      AND NOT EXISTS (
          SELECT 1
          FROM artifact_loss_archive_tombstones tombstone
          JOIN provider_recovery_notices notice ON notice.job_id=tombstone.archive_job_id
          JOIN provider_recovery_notice_parts archived
            ON archived.job_id=tombstone.archive_job_id
           AND archived.part_index=tombstone.archive_part_index
          WHERE tombstone.operation_id=item.operation_id
            AND tombstone.outbox_id=item.outbox_id
            AND tombstone.part_index=item.part_index
            AND tombstone.source_binding_hash=item.source_binding_hash
            AND tombstone.file_sha256=item.file_sha256
            AND tombstone.file_size=item.file_size
            AND tombstone.source_job_id=OLD.job_id
            AND tombstone.source_sender_agent_id=OLD.sender_agent_id
            AND tombstone.source_chat_id=OLD.chat_id
            AND tombstone.source_thread_id=OLD.thread_id
            AND tombstone.source_purpose=OLD.purpose
            AND tombstone.source_telegram_html IS OLD.telegram_html
            AND tombstone.source_status=OLD.status
            AND tombstone.source_attempt_count=OLD.attempt_count
            AND tombstone.source_telegram_message_id IS OLD.telegram_message_id
            AND tombstone.source_delivered_at IS OLD.delivered_at
            AND notice.outbox_id=tombstone.archive_outbox_id
            AND notice.telegram_html IS tombstone.source_telegram_html
            AND notice.delivery_status=tombstone.source_status
            AND notice.telegram_message_id IS tombstone.source_telegram_message_id
            AND notice.saved_at=tombstone.archive_saved_at
            AND archived.outbox_id=tombstone.archive_outbox_id
            AND archived.telegram_html IS tombstone.source_part_telegram_html
            AND archived.telegram_message_id IS tombstone.source_part_telegram_message_id
            AND archived.delivered_at IS tombstone.source_part_delivered_at
            AND archived.part_type=tombstone.source_part_type
            AND archived.file_path IS tombstone.source_file_path
            AND archived.file_name IS tombstone.source_file_name
            AND archived.file_size=tombstone.file_size
            AND archived.file_sha256=tombstone.file_sha256
            AND archived.receipt_validation_version=tombstone.source_part_receipt_validation_version
      )
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact outbox cannot be deleted'); END;

CREATE TRIGGER artifact_loss_source_job_no_delete
BEFORE DELETE ON provider_jobs
WHEN EXISTS (
    SELECT 1
    FROM artifact_loss_items item JOIN telegram_outbox outbox USING(outbox_id)
    WHERE outbox.job_id=OLD.job_id
      AND NOT EXISTS (
          SELECT 1
          FROM artifact_loss_archive_tombstones tombstone
          JOIN provider_recovery_notices notice ON notice.job_id=tombstone.archive_job_id
          JOIN provider_recovery_notice_parts archived
            ON archived.job_id=tombstone.archive_job_id
           AND archived.part_index=tombstone.archive_part_index
          WHERE tombstone.operation_id=item.operation_id
            AND tombstone.outbox_id=item.outbox_id
            AND tombstone.part_index=item.part_index
            AND tombstone.source_binding_hash=item.source_binding_hash
            AND tombstone.file_sha256=item.file_sha256
            AND tombstone.file_size=item.file_size
            AND tombstone.source_job_id=OLD.job_id
            AND notice.outbox_id=tombstone.archive_outbox_id
            AND notice.saved_at=tombstone.archive_saved_at
            AND archived.outbox_id=tombstone.archive_outbox_id
            AND archived.part_index=tombstone.archive_part_index
            AND archived.file_size=tombstone.file_size
            AND archived.file_sha256=tombstone.file_sha256
      )
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
      AND NOT EXISTS (
          SELECT 1
          FROM artifact_loss_archive_tombstones tombstone
          JOIN provider_recovery_notices notice ON notice.job_id=tombstone.archive_job_id
          JOIN provider_recovery_notice_parts archived
            ON archived.job_id=tombstone.archive_job_id
           AND archived.part_index=tombstone.archive_part_index
          WHERE tombstone.operation_id=item.operation_id
            AND tombstone.outbox_id=item.outbox_id
            AND tombstone.part_index=item.part_index
            AND tombstone.source_binding_hash=item.source_binding_hash
            AND tombstone.file_sha256=item.file_sha256
            AND tombstone.file_size=item.file_size
            AND tombstone.source_topic_id=OLD.topic_id
            AND tombstone.source_project_id=OLD.project_id
            AND tombstone.source_topic_chat_id=OLD.chat_id
            AND tombstone.source_topic_thread_id=OLD.thread_id
            AND notice.outbox_id=tombstone.archive_outbox_id
            AND notice.saved_at=tombstone.archive_saved_at
            AND archived.outbox_id=tombstone.archive_outbox_id
            AND archived.part_index=tombstone.archive_part_index
            AND archived.file_size=tombstone.file_size
            AND archived.file_sha256=tombstone.file_sha256
      )
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact topic cannot be deleted'); END;
"""


_SCHEMA_OBJECT = re.compile(r"CREATE\s+(TABLE|TRIGGER)\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.I)


def _normalise_schema_sql(value: str) -> str:
    """Compare DDL independent of layout, but not of its SQL semantics."""
    return re.sub(r"\s+", " ", value).strip().removesuffix(";").rstrip()


def _expected_schema_objects() -> dict[tuple[str, str], str]:
    """Return canonical DDL for every schema-59 object owned by this migration."""
    result: dict[tuple[str, str], str] = {}
    statement = ""
    for line in ARTIFACT_LOSS_SCHEMA.splitlines(keepends=True):
        # SQLite does not retain a leading standalone comment in sqlite_master.sql.
        if line.lstrip().startswith("--"):
            continue
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        match = _SCHEMA_OBJECT.match(statement.lstrip())
        if match is not None:
            key = (match.group(1).lower(), match.group(2))
            if key in result:
                raise RuntimeError("duplicate schema-59 object definition")
            result[key] = _normalise_schema_sql(statement)
        statement = ""
    if statement.strip() or set(name for _, name in result) != (
        ARTIFACT_LOSS_TABLES | ARTIFACT_LOSS_TRIGGER_NAMES
    ):
        raise RuntimeError("schema-59 object definitions are incomplete")
    return result


ARTIFACT_LOSS_SCHEMA_OBJECTS = _expected_schema_objects()


def has_complete_artifact_loss_schema(connection: sqlite3.Connection) -> bool:
    """Check the bounded structural prerequisites for schema-59 inventory."""
    expected_columns = {
        "artifact_loss_operations": ARTIFACT_LOSS_OPERATION_COLUMNS,
        "artifact_loss_items": ARTIFACT_LOSS_ITEM_COLUMNS,
        "artifact_loss_archive_tombstones": ARTIFACT_LOSS_ARCHIVE_TOMBSTONE_COLUMNS,
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
    tombstone_foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(artifact_loss_archive_tombstones)"
    ).fetchall()
    if {(row[3], row[4]) for row in tombstone_foreign_keys if row[2] == "artifact_loss_items"} != {
        ("operation_id", "operation_id"),
        ("outbox_id", "outbox_id"),
        ("part_index", "part_index"),
    }:
        return False
    placeholders = ",".join("?" for _ in ARTIFACT_LOSS_TRIGGER_NAMES)
    triggers = {
        row[0]
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ({placeholders})",
            tuple(ARTIFACT_LOSS_TRIGGER_NAMES),
        )
    }
    if triggers != ARTIFACT_LOSS_TRIGGER_NAMES:
        return False
    for (object_type, name), expected_sql in ARTIFACT_LOSS_SCHEMA_OBJECTS.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
        ).fetchone()
        if (
            row is None
            or not isinstance(row[0], str)
            or _normalise_schema_sql(row[0]) != expected_sql
        ):
            return False
    return True
