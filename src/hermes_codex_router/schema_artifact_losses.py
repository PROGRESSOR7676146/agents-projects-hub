"""Schema 59: immutable, owner-attested loss records for absent spool artifacts.

This is deliberately not delivery evidence.  It neither changes the outbox nor
authorizes a sender, replay, cleanup, or provider action.
"""

ARTIFACT_LOSS_TABLES = frozenset(("artifact_loss_operations", "artifact_loss_items"))
ARTIFACT_LOSS_TRIGGER_NAMES = frozenset(
    (
        "artifact_loss_operations_append_only",
        "artifact_loss_operations_no_update",
        "artifact_loss_operations_no_delete",
        "artifact_loss_items_insert_guard",
        "artifact_loss_items_no_update",
        "artifact_loss_items_no_delete",
        "artifact_loss_source_part_identity_immutable",
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

-- A late receipt may still settle an unknown send.  Freeze only the artifact
-- identity required by this ledger, not receipt/projection fields.
CREATE TRIGGER artifact_loss_source_part_identity_immutable
BEFORE UPDATE OF outbox_id,part_index,part_type,file_path,file_name,file_size,file_sha256
ON telegram_outbox_parts
WHEN EXISTS (
    SELECT 1 FROM artifact_loss_items item
    WHERE item.outbox_id=OLD.outbox_id AND item.part_index=OLD.part_index
)
AND (
    NEW.outbox_id IS NOT OLD.outbox_id
    OR NEW.part_index IS NOT OLD.part_index
    OR NEW.part_type IS NOT OLD.part_type
    OR NEW.file_path IS NOT OLD.file_path
    OR NEW.file_name IS NOT OLD.file_name
    OR NEW.file_size IS NOT OLD.file_size
    OR NEW.file_sha256 IS NOT OLD.file_sha256
)
BEGIN SELECT RAISE(ABORT, 'declared-loss artifact identity is immutable'); END;
"""
