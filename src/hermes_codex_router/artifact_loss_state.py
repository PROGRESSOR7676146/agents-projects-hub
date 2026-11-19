"""Exact local owner declaration of an absent Hub-spooled document.

The ledger records an owner decision about recovery coverage.  It is not a
Telegram receipt and does not alter delivery, queue, or provider state.
HubState supplies the only read/write transaction boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import artifact_capabilities
from .delivery_receipts import DeliveryReceipts

_HEX = re.compile(r"[0-9a-f]{64}")
_GENESIS = "0" * 64
_REASON = "irrecoverable_after_search"
_AUTHORITY = "local_owner_cli"


@dataclass(frozen=True, slots=True)
class ArtifactLossTarget:
    outbox_id: str
    part_index: int


@dataclass(frozen=True, slots=True)
class ArtifactLossPreview:
    operation_id: str
    snapshot: str
    previous_commit_hash: str
    target_count: int
    recovery_audit_sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactLossRecord:
    operation_id: str
    sequence: int
    snapshot: str
    commit_hash: str
    target_count: int
    recovery_audit_sha256: str


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _valid_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (AttributeError, ValueError):
        return False


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


class ArtifactLossState:
    """One immutable loss ledger on the caller-owned SQLite connection."""

    def __init__(self, db: sqlite3.Connection, state_error: Callable[[str], Exception]) -> None:
        self.db = db
        self.error = state_error

    @staticmethod
    def _normalise_targets(targets: Iterable[ArtifactLossTarget]) -> tuple[ArtifactLossTarget, ...]:
        values = tuple(targets)
        if not values:
            raise ValueError("at least one artifact-loss target is required")
        normalised: list[ArtifactLossTarget] = []
        seen: set[tuple[str, int]] = set()
        for target in values:
            if (
                not isinstance(target, ArtifactLossTarget)
                or not isinstance(target.outbox_id, str)
                or not 1 <= len(target.outbox_id) <= 128
                or type(target.part_index) is not int
                or target.part_index <= 0
            ):
                raise ValueError("invalid artifact-loss target")
            key = (target.outbox_id, target.part_index)
            if key in seen:
                raise ValueError("duplicate artifact-loss target")
            seen.add(key)
            normalised.append(target)
        return tuple(sorted(normalised, key=lambda value: (value.outbox_id, value.part_index)))

    def _require_transaction(self) -> None:
        if not self.db.in_transaction:
            raise self.error("artifact loss operation requires a state-owned transaction")

    def _require_declaration_capability(self) -> None:
        if not artifact_capabilities.EXPLICIT_ARTIFACT_LOSS_DECLARATION:
            raise self.error("this release does not permit artifact-loss declarations")

    def _require_audit(self, digest: str) -> str:
        if not _valid_sha(digest):
            raise self.error("artifact loss requires an exact recovery-audit SHA-256")
        return digest

    def _canonical_spool_root(self, spool_root: Path) -> Path:
        root = spool_root.expanduser().absolute()
        try:
            canonical = root.resolve(strict=True)
            metadata = os.lstat(canonical)
        except OSError as exc:
            raise self.error("artifact spool root is unavailable") from exc
        if root != canonical or root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise self.error("artifact spool root is not canonical")
        return canonical

    def _artifact_missing(self, raw_path: object, root: Path) -> bool:
        if not isinstance(raw_path, str) or not raw_path:
            raise self.error("artifact loss source has no file path")
        path = Path(raw_path)
        if not path.is_absolute() or path == root:
            raise self.error("artifact loss source path is unsafe")
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise self.error("artifact loss source escapes its spool root") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise self.error("artifact loss source path is unsafe")
        current = root
        for component in relative.parts:
            current = current / component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                return True
            except OSError as exc:
                raise self.error("artifact loss source cannot be inspected") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise self.error("artifact loss source contains a symlink")
            if current != path:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise self.error("artifact loss source parent is unsafe")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise self.error("artifact loss source is not a regular file")
            return False
        raise self.error("artifact loss source cannot be inspected")

    def _target_snapshot(self, target: ArtifactLossTarget, spool_root: Path) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT outbox.outbox_id,outbox.status,outbox.attempt_count,outbox.lease_owner,
                      outbox.lease_token,outbox.lease_expires_at,
                      part.part_index,part.part_type,part.file_path,part.file_name,
                      part.file_size,part.file_sha256
               FROM telegram_outbox outbox
               JOIN telegram_outbox_parts part USING(outbox_id)
               WHERE outbox.outbox_id=? AND part.part_index=?""",
            (target.outbox_id, target.part_index),
        ).fetchone()
        if row is None:
            raise self.error("artifact loss target is unavailable")
        if (
            row["part_type"] != "document"
            or row["status"] not in {"delivered", "unknown", "failed"}
            or row["lease_owner"] is not None
            or row["lease_token"] is not None
            or row["lease_expires_at"] is not None
            or (row["status"] == "failed" and row["attempt_count"] != 20)
            or not isinstance(row["file_name"], str)
            or not isinstance(row["file_size"], int)
            or not 1 <= row["file_size"] <= 50 * 1024 * 1024
            or not _valid_sha(row["file_sha256"])
        ):
            raise self.error("artifact loss target is not an eligible parked document")
        if (
            self.db.execute(
                """SELECT 1 FROM telegram_artifact_retirements
               WHERE outbox_id=? AND part_index=?""",
                (target.outbox_id, target.part_index),
            ).fetchone()
            is not None
        ):
            raise self.error("receipt-retired artifact cannot be declared lost")
        if (
            self.db.execute(
                """SELECT 1 FROM artifact_loss_items WHERE outbox_id=? AND part_index=?""",
                (target.outbox_id, target.part_index),
            ).fetchone()
            is not None
        ):
            raise self.error("artifact already has an immutable loss declaration")
        binding = DeliveryReceipts(self.db, self.error)._binding(
            "final", target.outbox_id, target.part_index
        )
        if not _valid_sha(binding):
            raise self.error("artifact loss source binding is unavailable")
        if not self._artifact_missing(row["file_path"], spool_root):
            raise self.error("artifact loss source is still present")
        return {
            "outbox_id": target.outbox_id,
            "part_index": target.part_index,
            "source_binding_hash": binding,
            "file_sha256": str(row["file_sha256"]),
            "file_size": int(row["file_size"]),
        }

    @staticmethod
    def _snapshot_value(
        *, previous_commit_hash: str, recovery_audit_sha256: str, targets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "previous_commit_hash": previous_commit_hash,
            "recovery_audit_sha256": recovery_audit_sha256,
            "targets": targets,
        }

    @staticmethod
    def _commit_value(
        *,
        operation_id: str,
        sequence: int,
        previous_commit_hash: str,
        snapshot_hash: str,
        recovery_audit_sha256: str,
        applied_at: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "operation_id": operation_id,
            "sequence": sequence,
            "previous_commit_hash": previous_commit_hash,
            "snapshot_hash": snapshot_hash,
            "recovery_audit_sha256": recovery_audit_sha256,
            "reason": _REASON,
            "authority": _AUTHORITY,
            "applied_at": applied_at,
        }

    def _items_for_operation(self, operation_id: str) -> list[dict[str, Any]]:
        return [
            {
                "outbox_id": str(row["outbox_id"]),
                "part_index": int(row["part_index"]),
                "source_binding_hash": str(row["source_binding_hash"]),
                "file_sha256": str(row["file_sha256"]),
                "file_size": int(row["file_size"]),
            }
            for row in self.db.execute(
                """SELECT outbox_id,part_index,source_binding_hash,file_sha256,file_size
                   FROM artifact_loss_items WHERE operation_id=?
                   ORDER BY outbox_id,part_index""",
                (operation_id,),
            )
        ]

    def ledger_head(self) -> tuple[int, str]:
        """Return and validate the full append-only chain on the current connection."""
        if (
            self.db.execute(
                """SELECT 1 FROM artifact_loss_items item
                   LEFT JOIN artifact_loss_operations operation
                     ON operation.operation_id=item.operation_id
                   WHERE operation.operation_id IS NULL LIMIT 1"""
            ).fetchone()
            is not None
        ):
            raise self.error("artifact loss ledger has an orphaned item")
        previous = _GENESIS
        sequence = 0
        rows = self.db.execute(
            """SELECT operation_id,sequence,previous_commit_hash,snapshot_hash,recovery_audit_sha256,
                      reason,authority,applied_at,commit_hash
               FROM artifact_loss_operations ORDER BY sequence"""
        ).fetchall()
        for row in rows:
            operation_id = row["operation_id"]
            if (
                not _valid_uuid(operation_id)
                or row["sequence"] != sequence + 1
                or row["previous_commit_hash"] != previous
                or row["reason"] != _REASON
                or row["authority"] != _AUTHORITY
                or not _valid_timestamp(row["applied_at"])
                or not _valid_sha(row["snapshot_hash"])
                or not _valid_sha(row["recovery_audit_sha256"])
                or not _valid_sha(row["commit_hash"])
            ):
                raise self.error("artifact loss ledger chain is invalid")
            items = self._items_for_operation(operation_id)
            if not items or any(
                not isinstance(item["outbox_id"], str)
                or not 1 <= len(item["outbox_id"]) <= 128
                or type(item["part_index"]) is not int
                or item["part_index"] <= 0
                or not _valid_sha(item["source_binding_hash"])
                or not _valid_sha(item["file_sha256"])
                or type(item["file_size"]) is not int
                or not 1 <= item["file_size"] <= 50 * 1024 * 1024
                for item in items
            ):
                raise self.error("artifact loss operation has no items")
            snapshot = _digest(
                self._snapshot_value(
                    previous_commit_hash=previous,
                    recovery_audit_sha256=str(row["recovery_audit_sha256"]),
                    targets=items,
                )
            )
            commit = _digest(
                self._commit_value(
                    operation_id=operation_id,
                    sequence=int(row["sequence"]),
                    previous_commit_hash=previous,
                    snapshot_hash=snapshot,
                    recovery_audit_sha256=str(row["recovery_audit_sha256"]),
                    applied_at=str(row["applied_at"]),
                )
            )
            if snapshot != row["snapshot_hash"] or commit != row["commit_hash"]:
                raise self.error("artifact loss ledger contents are invalid")
            sequence = int(row["sequence"])
            previous = str(row["commit_hash"])
        return sequence, previous

    def preview(
        self,
        targets: Iterable[ArtifactLossTarget],
        *,
        spool_root: Path,
        recovery_audit_sha256: str,
        operation_id: str | None = None,
    ) -> ArtifactLossPreview:
        self._require_transaction()
        self._require_declaration_capability()
        audit = self._require_audit(recovery_audit_sha256)
        try:
            identifier = (
                str(uuid.UUID(operation_id)) if operation_id is not None else str(uuid.uuid4())
            )
        except (ValueError, AttributeError) as exc:
            raise self.error("artifact loss operation id is invalid") from exc
        root = self._canonical_spool_root(spool_root)
        _, previous = self.ledger_head()
        try:
            normalised = self._normalise_targets(targets)
        except ValueError as exc:
            raise self.error(str(exc)) from exc
        entries = [self._target_snapshot(target, root) for target in normalised]
        snapshot = _digest(
            self._snapshot_value(
                previous_commit_hash=previous, recovery_audit_sha256=audit, targets=entries
            )
        )
        return ArtifactLossPreview(identifier, snapshot, previous, len(entries), audit)

    def _record(self, row: sqlite3.Row) -> ArtifactLossRecord:
        return ArtifactLossRecord(
            operation_id=str(row["operation_id"]),
            sequence=int(row["sequence"]),
            snapshot=str(row["snapshot_hash"]),
            commit_hash=str(row["commit_hash"]),
            target_count=len(self._items_for_operation(str(row["operation_id"]))),
            recovery_audit_sha256=str(row["recovery_audit_sha256"]),
        )

    def apply(
        self,
        targets: Iterable[ArtifactLossTarget],
        *,
        spool_root: Path,
        recovery_audit_sha256: str,
        expected_snapshot: str,
        operation_id: str,
        acknowledge_irrecoverable_loss: bool,
        applied_at: str,
    ) -> ArtifactLossRecord:
        self._require_transaction()
        self._require_declaration_capability()
        if acknowledge_irrecoverable_loss is not True:
            raise self.error("explicit acknowledgement of irrecoverable artifact loss is required")
        if not _valid_sha(expected_snapshot):
            raise self.error("exact artifact loss preview snapshot is required")
        if not _valid_timestamp(applied_at):
            raise self.error("artifact loss time must be timezone-aware")
        audit = self._require_audit(recovery_audit_sha256)
        try:
            identifier = str(uuid.UUID(operation_id))
        except (ValueError, AttributeError) as exc:
            raise self.error("artifact loss operation id is invalid") from exc
        try:
            normalised = self._normalise_targets(targets)
        except ValueError as exc:
            raise self.error(str(exc)) from exc
        prior = self.db.execute(
            "SELECT * FROM artifact_loss_operations WHERE operation_id=?", (identifier,)
        ).fetchone()
        if prior is not None:
            if (
                prior["snapshot_hash"] != expected_snapshot
                or prior["recovery_audit_sha256"] != audit
            ):
                raise self.error("artifact loss operation already has different immutable contents")
            # An uncertain caller may safely repeat its exact request, but a
            # malformed/partial historical ledger must never be reported as a
            # successful idempotent result.
            self.ledger_head()
            prior_targets = tuple(
                ArtifactLossTarget(str(item["outbox_id"]), int(item["part_index"]))
                for item in self._items_for_operation(identifier)
            )
            if prior_targets != normalised:
                raise self.error("artifact loss operation already has different immutable contents")
            return self._record(prior)
        root = self._canonical_spool_root(spool_root)
        sequence, previous = self.ledger_head()
        entries = [self._target_snapshot(target, root) for target in normalised]
        snapshot = _digest(
            self._snapshot_value(
                previous_commit_hash=previous, recovery_audit_sha256=audit, targets=entries
            )
        )
        if snapshot != expected_snapshot:
            raise self.error("artifact loss preview is stale; inspect a fresh preview")
        next_sequence = sequence + 1
        commit = _digest(
            self._commit_value(
                operation_id=identifier,
                sequence=next_sequence,
                previous_commit_hash=previous,
                snapshot_hash=snapshot,
                recovery_audit_sha256=audit,
                applied_at=applied_at,
            )
        )
        self.db.execute(
            """INSERT INTO artifact_loss_operations
               (operation_id,sequence,previous_commit_hash,snapshot_hash,recovery_audit_sha256,
                reason,authority,applied_at,commit_hash)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                next_sequence,
                previous,
                snapshot,
                audit,
                _REASON,
                _AUTHORITY,
                applied_at,
                commit,
            ),
        )
        self.db.executemany(
            """INSERT INTO artifact_loss_items
               (operation_id,outbox_id,part_index,source_binding_hash,file_sha256,file_size)
               VALUES(?,?,?,?,?,?)""",
            [
                (
                    identifier,
                    entry["outbox_id"],
                    entry["part_index"],
                    entry["source_binding_hash"],
                    entry["file_sha256"],
                    entry["file_size"],
                )
                for entry in entries
            ],
        )
        row = self.db.execute(
            "SELECT * FROM artifact_loss_operations WHERE operation_id=?", (identifier,)
        ).fetchone()
        assert row is not None
        return self._record(row)
