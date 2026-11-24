"""Read-only recovery coverage for Hub-spooled document artifacts.

This consumer deliberately distinguishes a locally owner-attested missing file
from receipt evidence.  It never changes the database, filesystem, delivery,
queue, or provider state.  Its ``complete_artifact_coverage`` result covers
only document artifacts represented by the inspected SQLite snapshot; callers
still own their complete recovery-set policy.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .artifact_loss_state import ArtifactLossState
from .delivery_receipts import DeliveryReceipts
from .schema_artifact_losses import ARTIFACT_LOSS_TABLES, has_complete_artifact_loss_schema

_HEX = re.compile(r"[0-9a-f]{64}")
_GENESIS = "0" * 64
# Schema 54 introduced the immutable final-send attempt/receipt evidence this
# reader needs for every permitted missing-document classification.
_MIN_RECEIPT_SCHEMA = 54
# Schema 60 adds only topic panel UI tables (ADR 0110) and schema 61 only
# agent mail tables whose letters live in the database, not in the artifact
# spool (ADR 0112); outbox and receipt evidence are unchanged. Review this
# bound for every later schema.
_MAX_SCHEMA = 61

Availability = Literal[
    "present_required",
    "receipt_retired",
    "receipt_verified_unknown",
    "explicit_loss",
    "unresolved_missing",
]


class ArtifactRecoveryInventoryError(RuntimeError):
    """The snapshot or its local artifact inventory cannot be safely inspected."""


@dataclass(frozen=True, slots=True)
class ArtifactRecoveryEntry:
    outbox_id: str
    part_index: int
    availability: Availability
    file_size: int | None
    file_sha256: str | None
    file_path: Path | None


@dataclass(frozen=True, slots=True)
class ArtifactRecoveryInventory:
    schema_version: int
    snapshot_sha256: str
    ledger_sequence: int | None
    ledger_head: str | None
    ledger_valid: bool
    coverage: Literal["incomplete", "degraded_explicit_loss", "complete_artifact_coverage"]
    entries: tuple[ArtifactRecoveryEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        result = {
            "present_required": 0,
            "receipt_retired": 0,
            "receipt_verified_unknown": 0,
            "explicit_loss": 0,
            "unresolved_missing": 0,
        }
        for entry in self.entries:
            result[entry.availability] += 1
        return result


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _file_identity(item: os.stat_result) -> tuple[int, int, int, int]:
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns


def _stable_regular(path: Path, *, label: str) -> tuple[Path, os.stat_result]:
    raw = path.expanduser().absolute()
    try:
        resolved = raw.resolve(strict=True)
        metadata = os.lstat(resolved)
    except OSError as exc:
        raise ArtifactRecoveryInventoryError(f"{label} is unavailable") from exc
    if raw != resolved or raw.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ArtifactRecoveryInventoryError(f"{label} is not a canonical regular file")
    return resolved, metadata


def _digest_regular(path: Path, *, expected: os.stat_result, label: str) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ArtifactRecoveryInventoryError(f"{label} is unavailable") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactRecoveryInventoryError(f"{label} is not a regular file")
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(stream.fileno())
    except BaseException:
        raise
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise ArtifactRecoveryInventoryError(f"{label} changed during inspection") from exc
    if (
        _file_identity(before) != _file_identity(after)
        or _file_identity(before) != _file_identity(current)
        or _file_identity(before) != _file_identity(expected)
    ):
        raise ArtifactRecoveryInventoryError(f"{label} changed during inspection")
    return digest.hexdigest()


def _canonical_spool_root(spool_root: Path) -> Path:
    raw = spool_root.expanduser().absolute()
    try:
        resolved = raw.resolve(strict=True)
        metadata = os.lstat(resolved)
    except OSError as exc:
        raise ArtifactRecoveryInventoryError("artifact spool root is unavailable") from exc
    if raw != resolved or raw.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactRecoveryInventoryError("artifact spool root is not canonical")
    return resolved


def _assert_no_snapshot_sidecars(snapshot: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(snapshot) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ArtifactRecoveryInventoryError("artifact recovery snapshot has SQLite sidecars")


def _file_status(
    raw_path: object, spool_root: Path, expected_size: object, expected_sha256: object
) -> tuple[Literal["present", "missing", "unsafe"], Path | None]:
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or type(expected_size) is not int
        or not 1 <= expected_size <= 50 * 1024 * 1024
        or not _valid_sha(expected_sha256)
    ):
        return "unsafe", None
    path = Path(raw_path)
    if not path.is_absolute() or path == spool_root:
        return "unsafe", None
    try:
        relative = path.relative_to(spool_root)
    except ValueError:
        return "unsafe", None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return "unsafe", None
    current = spool_root
    for component in relative.parts:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return "missing", path
        except OSError:
            return "unsafe", None
        if stat.S_ISLNK(metadata.st_mode):
            return "unsafe", None
        if current != path:
            if not stat.S_ISDIR(metadata.st_mode):
                return "unsafe", None
            continue
        if not stat.S_ISREG(metadata.st_mode):
            return "unsafe", None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return "unsafe", None
        try:
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                    return "unsafe", None
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                after = os.fstat(stream.fileno())
        except OSError:
            return "unsafe", None
        try:
            current_metadata = os.lstat(path)
        except OSError:
            return "unsafe", None
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(before) != _file_identity(current_metadata)
            or digest.hexdigest() != expected_sha256
        ):
            return "unsafe", None
        return "present", path
    return "unsafe", None


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _receipt_retired(
    connection: sqlite3.Connection,
    receipts: DeliveryReceipts,
    row: sqlite3.Row,
) -> bool:
    record = connection.execute(
        """SELECT attempt_id,binding_hash,telegram_message_id,delivered_at,file_path,file_name,
                  file_size,file_sha256,retired_at
           FROM telegram_artifact_retirements WHERE outbox_id=? AND part_index=?""",
        (row["outbox_id"], row["part_index"]),
    ).fetchall()
    if len(record) != 1:
        return False
    snapshot = receipts.artifact_retirement_snapshot(str(row["outbox_id"]), int(row["part_index"]))
    if snapshot is None:
        return False
    expected = tuple(snapshot[key] for key in record[0].keys())
    return tuple(record[0]) == expected


def _receipt_verified_unknown(receipts: DeliveryReceipts, row: sqlite3.Row) -> bool:
    if (
        row["status"] != "unknown"
        or row["receipt_validation_version"] != 1
        or type(row["telegram_message_id"]) is not int
        or row["telegram_message_id"] <= 0
        or not isinstance(row["part_delivered_at"], str)
        or not row["part_delivered_at"]
    ):
        return False
    return (
        receipts._exact_final_attempt(  # noqa: SLF001 - canonical receipt verifier owns this check.
            str(row["outbox_id"]),
            int(row["part_index"]),
            int(row["telegram_message_id"]),
            str(row["part_delivered_at"]),
        )
        is not None
    )


def _exact_archived_attempt(
    connection: sqlite3.Connection,
    *,
    outbox_id: str,
    part_index: int,
    telegram_message_id: object,
    delivered_at: object,
    binding_hash: object,
) -> bool:
    if (
        type(telegram_message_id) is not int
        or telegram_message_id <= 0
        or not isinstance(delivered_at, str)
        or not delivered_at
        or not _valid_sha(binding_hash)
    ):
        return False
    rows = connection.execute(
        """SELECT attempt.binding_hash
           FROM telegram_send_attempts attempt
           JOIN telegram_send_receipts receipt USING(attempt_id)
           WHERE attempt.target_kind='final' AND attempt.target_id=? AND attempt.part_index=?
             AND receipt.telegram_message_id=? AND receipt.recorded_at=?
             AND NOT EXISTS (
                 SELECT 1 FROM telegram_send_attempts newer
                 WHERE newer.target_kind='final' AND newer.target_id=attempt.target_id
                   AND newer.part_index=attempt.part_index AND newer.sequence>attempt.sequence
             )""",
        (outbox_id, part_index, telegram_message_id, delivered_at),
    ).fetchall()
    return len(rows) == 1 and rows[0]["binding_hash"] == binding_hash


def _receipt_retired_tombstone(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if row["source_status"] != "delivered" or row["source_part_receipt_validation_version"] != 1:
        return False
    records = connection.execute(
        """SELECT attempt_id,binding_hash,telegram_message_id,delivered_at,file_path,file_name,
                  file_size,file_sha256,retired_at
           FROM telegram_artifact_retirements WHERE outbox_id=? AND part_index=?""",
        (row["outbox_id"], row["part_index"]),
    ).fetchall()
    if len(records) != 1:
        return False
    record = records[0]
    if (
        record["binding_hash"] != row["source_binding_hash"]
        or record["telegram_message_id"] != row["source_part_telegram_message_id"]
        or record["delivered_at"] != row["source_part_delivered_at"]
        or record["file_path"] != row["source_file_path"]
        or record["file_name"] != row["source_file_name"]
        or record["file_size"] != row["file_size"]
        or record["file_sha256"] != row["file_sha256"]
        or record["retired_at"] != row["source_delivered_at"]
    ):
        return False
    attempt = connection.execute(
        """SELECT target_kind,target_id,part_index,binding_hash
           FROM telegram_send_attempts WHERE attempt_id=?""",
        (record["attempt_id"],),
    ).fetchone()
    return (
        attempt is not None
        and attempt["target_kind"] == "final"
        and attempt["target_id"] == row["outbox_id"]
        and attempt["part_index"] == row["part_index"]
        and attempt["binding_hash"] == row["source_binding_hash"]
        and _exact_archived_attempt(
            connection,
            outbox_id=str(row["outbox_id"]),
            part_index=int(row["part_index"]),
            telegram_message_id=row["source_part_telegram_message_id"],
            delivered_at=row["source_part_delivered_at"],
            binding_hash=row["source_binding_hash"],
        )
    )


def _receipt_verified_unknown_tombstone(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    return row["source_status"] == "unknown" and (
        row["source_part_receipt_validation_version"] == 1
        and _exact_archived_attempt(
            connection,
            outbox_id=str(row["outbox_id"]),
            part_index=int(row["part_index"]),
            telegram_message_id=row["source_part_telegram_message_id"],
            delivered_at=row["source_part_delivered_at"],
            binding_hash=row["source_binding_hash"],
        )
    )


def _eligible_explicit_loss_source(row: sqlite3.Row) -> bool:
    return (
        row["status"] in {"delivered", "unknown", "failed"}
        and row["lease_owner"] is None
        and row["lease_token"] is None
        and row["lease_expires_at"] is None
        and (row["status"] != "failed" or row["attempt_count"] == 20)
    )


def _explicit_loss(
    connection: sqlite3.Connection,
    receipts: DeliveryReceipts,
    row: sqlite3.Row,
    *,
    ledger_valid: bool,
) -> bool:
    # Schema 54--58 snapshots have the receipt evidence needed for the
    # other classifications but predate the loss ledger altogether.  They
    # must remain readable, with any otherwise-unaccounted missing file
    # explicitly unresolved rather than causing a lookup of a schema-59 table.
    if (
        not ledger_valid
        or not _table_exists(connection, "artifact_loss_items")
        or not _eligible_explicit_loss_source(row)
    ):
        return False
    if (
        _table_exists(connection, "telegram_artifact_retirements")
        and connection.execute(
            "SELECT 1 FROM telegram_artifact_retirements WHERE outbox_id=? AND part_index=?",
            (row["outbox_id"], row["part_index"]),
        ).fetchone()
        is not None
    ):
        return False
    binding = receipts._binding("final", str(row["outbox_id"]), int(row["part_index"]))
    if not _valid_sha(binding):
        return False
    item = connection.execute(
        """SELECT item.source_binding_hash,item.file_sha256,item.file_size
           FROM artifact_loss_items item
           JOIN artifact_loss_operations operation USING(operation_id)
           WHERE item.outbox_id=? AND item.part_index=?""",
        (row["outbox_id"], row["part_index"]),
    ).fetchone()
    return item is not None and (
        item["source_binding_hash"],
        item["file_sha256"],
        item["file_size"],
    ) == (binding, row["file_sha256"], row["file_size"])


def _tombstoned_loss_availability(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    spool_root: Path,
    *,
    retirement_table: bool,
    ledger_valid: bool,
) -> tuple[Availability, Path | None]:
    status, path = _file_status(
        row["source_file_path"], spool_root, row["file_size"], row["file_sha256"]
    )
    if status == "present":
        return "present_required", path
    if status == "missing" and retirement_table and _receipt_retired_tombstone(connection, row):
        return "receipt_retired", path
    if status == "missing" and _receipt_verified_unknown_tombstone(connection, row):
        return "receipt_verified_unknown", path
    if status == "missing" and ledger_valid:
        return "explicit_loss", path
    return "unresolved_missing", path


def _ledger_status(
    connection: sqlite3.Connection, *, schema_version: int
) -> tuple[int | None, str | None, bool]:
    if schema_version < 59:
        if any(_table_exists(connection, table) for table in ARTIFACT_LOSS_TABLES):
            return None, None, False
        return None, None, True
    if not has_complete_artifact_loss_schema(connection):
        return None, None, False
    try:
        sequence, head = ArtifactLossState(connection, ArtifactRecoveryInventoryError).ledger_head()
    except (ArtifactRecoveryInventoryError, sqlite3.Error):
        return None, None, False
    return sequence, head if sequence else _GENESIS, True


def inspect_artifact_recovery_inventory(
    snapshot_path: Path, spool_root: Path
) -> ArtifactRecoveryInventory:
    """Inspect one closed SQLite snapshot and its current local spool without mutation.

    A snapshot must have no SQLite sidecars.  The routine rehashes it before
    and after SQL inspection, so a replaced or concurrently rewritten input is
    rejected rather than reported as recovery evidence.
    """
    snapshot, metadata = _stable_regular(snapshot_path, label="artifact recovery snapshot")
    _assert_no_snapshot_sidecars(snapshot)
    before_digest = _digest_regular(snapshot, expected=metadata, label="artifact recovery snapshot")
    root = _canonical_spool_root(spool_root)
    try:
        connection = sqlite3.connect(
            snapshot.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5.0
        )
    except sqlite3.Error as exc:
        raise ArtifactRecoveryInventoryError("artifact recovery snapshot is unavailable") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ArtifactRecoveryInventoryError(
                "artifact recovery snapshot integrity check failed"
            )
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if not _MIN_RECEIPT_SCHEMA <= schema_version <= _MAX_SCHEMA:
            raise ArtifactRecoveryInventoryError("artifact recovery schema is unsupported")
        retirement_table = _table_exists(connection, "telegram_artifact_retirements")
        if schema_version >= 58 and not retirement_table:
            raise ArtifactRecoveryInventoryError(
                "artifact recovery retirement ledger is unavailable"
            )
        sequence, head, ledger_valid = _ledger_status(connection, schema_version=schema_version)
        receipts = DeliveryReceipts(connection, ArtifactRecoveryInventoryError)
        rows = connection.execute(
            """SELECT outbox.outbox_id,outbox.status,outbox.attempt_count,outbox.lease_owner,
                      outbox.lease_token,outbox.lease_expires_at,
                      part.part_index,part.telegram_message_id,part.receipt_validation_version,
                      part.delivered_at AS part_delivered_at,part.file_path,part.file_name,
                      part.file_size,part.file_sha256
               FROM telegram_outbox outbox JOIN telegram_outbox_parts part USING(outbox_id)
               WHERE part.part_type='document' ORDER BY outbox.outbox_id,part.part_index"""
        ).fetchall()
        entries: list[ArtifactRecoveryEntry] = []
        for row in rows:
            status, path = _file_status(
                row["file_path"], root, row["file_size"], row["file_sha256"]
            )
            if status == "present":
                availability: Availability = "present_required"
            elif (
                status == "missing"
                and retirement_table
                and _receipt_retired(connection, receipts, row)
            ):
                availability = "receipt_retired"
            elif status == "missing" and _receipt_verified_unknown(receipts, row):
                availability = "receipt_verified_unknown"
            elif status == "missing" and _explicit_loss(
                connection, receipts, row, ledger_valid=ledger_valid
            ):
                availability = "explicit_loss"
            else:
                availability = "unresolved_missing"
            entries.append(
                ArtifactRecoveryEntry(
                    outbox_id=str(row["outbox_id"]),
                    part_index=int(row["part_index"]),
                    availability=availability,
                    file_size=row["file_size"] if type(row["file_size"]) is int else None,
                    file_sha256=row["file_sha256"] if _valid_sha(row["file_sha256"]) else None,
                    file_path=path,
                )
            )
        has_loss_items = _table_exists(connection, "artifact_loss_items")
        has_tombstones = _table_exists(connection, "artifact_loss_archive_tombstones")
        if has_tombstones:
            tombstones = connection.execute(
                """SELECT tombstone.* FROM artifact_loss_archive_tombstones tombstone
                   WHERE NOT EXISTS (
                       SELECT 1 FROM telegram_outbox_parts part
                       WHERE part.outbox_id=tombstone.outbox_id
                         AND part.part_index=tombstone.part_index
                   )
                   ORDER BY tombstone.outbox_id,tombstone.part_index"""
            ).fetchall()
            for tombstone in tombstones:
                availability, path = _tombstoned_loss_availability(
                    connection,
                    tombstone,
                    root,
                    retirement_table=retirement_table,
                    ledger_valid=ledger_valid,
                )
                entries.append(
                    ArtifactRecoveryEntry(
                        outbox_id=str(tombstone["outbox_id"]),
                        part_index=int(tombstone["part_index"]),
                        availability=availability,
                        file_size=tombstone["file_size"]
                        if type(tombstone["file_size"]) is int
                        else None,
                        file_sha256=tombstone["file_sha256"]
                        if _valid_sha(tombstone["file_sha256"])
                        else None,
                        file_path=path,
                    )
                )
        if has_loss_items:
            absent = (
                connection.execute(
                    """SELECT item.outbox_id,item.part_index,item.file_size,item.file_sha256
                   FROM artifact_loss_items item
                   WHERE NOT EXISTS (
                       SELECT 1 FROM telegram_outbox_parts part
                       WHERE part.outbox_id=item.outbox_id AND part.part_index=item.part_index
                         AND part.part_type='document'
                   )
                     AND NOT EXISTS (
                       SELECT 1 FROM artifact_loss_archive_tombstones tombstone
                       WHERE tombstone.outbox_id=item.outbox_id
                         AND tombstone.part_index=item.part_index
                   )
                   ORDER BY item.outbox_id,item.part_index"""
                ).fetchall()
                if has_tombstones
                else connection.execute(
                    """SELECT item.outbox_id,item.part_index,item.file_size,item.file_sha256
                   FROM artifact_loss_items item
                   WHERE NOT EXISTS (
                       SELECT 1 FROM telegram_outbox_parts part
                       WHERE part.outbox_id=item.outbox_id AND part.part_index=item.part_index
                         AND part.part_type='document'
                   )
                   ORDER BY item.outbox_id,item.part_index"""
                ).fetchall()
            )
            for item in absent:
                entries.append(
                    ArtifactRecoveryEntry(
                        outbox_id=str(item["outbox_id"]),
                        part_index=int(item["part_index"]),
                        availability="unresolved_missing",
                        file_size=item["file_size"] if type(item["file_size"]) is int else None,
                        file_sha256=item["file_sha256"]
                        if _valid_sha(item["file_sha256"])
                        else None,
                        file_path=None,
                    )
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    after_digest = _digest_regular(snapshot, expected=metadata, label="artifact recovery snapshot")
    _assert_no_snapshot_sidecars(snapshot)
    if after_digest != before_digest:
        raise ArtifactRecoveryInventoryError("artifact recovery snapshot changed during inspection")
    unresolved = sum(entry.availability == "unresolved_missing" for entry in entries)
    explicit = sum(entry.availability == "explicit_loss" for entry in entries)
    coverage: Literal["incomplete", "degraded_explicit_loss", "complete_artifact_coverage"]
    if unresolved or not ledger_valid:
        coverage = "incomplete"
    elif explicit:
        coverage = "degraded_explicit_loss"
    else:
        coverage = "complete_artifact_coverage"
    return ArtifactRecoveryInventory(
        schema_version=schema_version,
        snapshot_sha256=before_digest,
        ledger_sequence=sequence,
        ledger_head=head,
        ledger_valid=ledger_valid,
        coverage=coverage,
        entries=tuple(sorted(entries, key=lambda entry: (entry.outbox_id, entry.part_index))),
    )
