"""Local-only owner interface for irreversible artifact-loss attestations.

This command deliberately has no Hub configuration, Telegram, provider, or
service-manager input.  A private target-selection file keeps a large batch out
of process arguments and shell history.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .artifact_loss_state import ArtifactLossTarget
from .artifact_recovery_inventory import inspect_artifact_recovery_inventory
from .state import HubState, StateError

_SELECTION_LIMIT = 1024 * 1024


def _file_identity(item: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
        stat.S_IMODE(item.st_mode),
        item.st_uid,
        item.st_nlink,
    )


def _is_private_regular(item: os.stat_result) -> bool:
    return (
        stat.S_ISREG(item.st_mode)
        and item.st_uid == os.getuid()
        and item.st_nlink == 1
        and stat.S_IMODE(item.st_mode) == 0o600
        and item.st_size <= _SELECTION_LIMIT
    )


def add_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser(
        "artifact-loss",
        help="inspect or record an explicit local-owner loss of absent Hub document artifacts",
    )
    actions = command.add_subparsers(dest="artifact_loss_command", required=True)

    inventory = actions.add_parser("inventory", help="read one closed snapshot without changing it")
    inventory.add_argument("snapshot", type=Path)
    inventory.add_argument("--spool-root", required=True, type=Path)

    preview = actions.add_parser("preview", help="preview one private batch without changing state")
    preview.add_argument("state", type=Path)
    preview.add_argument("--spool-root", required=True, type=Path)
    preview.add_argument("--targets", required=True, type=Path)
    preview.add_argument("--recovery-audit-sha256", required=True)
    preview.add_argument("--operation-id")

    apply = actions.add_parser("apply", help="append one irreversible local-owner loss record")
    apply.add_argument("state", type=Path)
    apply.add_argument("--spool-root", required=True, type=Path)
    apply.add_argument("--targets", required=True, type=Path)
    apply.add_argument("--recovery-audit-sha256", required=True)
    apply.add_argument("--snapshot", required=True)
    apply.add_argument("--operation-id", required=True)
    apply.add_argument("--acknowledge-irrecoverable-loss", action="store_true")


def _private_regular(path: Path) -> tuple[Path, os.stat_result]:
    raw = path.expanduser().absolute()
    try:
        resolved = raw.resolve(strict=True)
        metadata = os.lstat(resolved)
    except OSError as exc:
        raise StateError("artifact-loss target selection is unavailable") from exc
    if raw != resolved or raw.is_symlink() or not _is_private_regular(metadata):
        raise StateError(
            "artifact-loss target selection must be one private mode-0600 regular file"
        )
    return resolved, metadata


def _load_targets(path: Path) -> tuple[ArtifactLossTarget, ...]:
    selection, expected = _private_regular(path)
    try:
        descriptor = os.open(selection, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise StateError("artifact-loss target selection is unavailable") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(_SELECTION_LIMIT + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise StateError("artifact-loss target selection cannot be read") from exc
    try:
        current = os.lstat(selection)
    except OSError as exc:
        raise StateError("artifact-loss target selection changed during read") from exc
    if (
        len(raw) > _SELECTION_LIMIT
        or not all(_is_private_regular(item) for item in (expected, before, after, current))
        or _file_identity(before) != _file_identity(after)
        or _file_identity(before) != _file_identity(current)
        or _file_identity(before) != _file_identity(expected)
    ):
        raise StateError("artifact-loss target selection changed during read")
    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("artifact-loss target selection is not valid UTF-8 JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "targets"}
        or document["version"] != 1
    ):
        raise StateError("artifact-loss target selection has an unsupported format")
    raw_targets = document["targets"]
    if not isinstance(raw_targets, list):
        raise StateError("artifact-loss target selection has an unsupported format")
    targets: list[ArtifactLossTarget] = []
    for item in raw_targets:
        if not isinstance(item, dict) or set(item) != {"outbox_id", "part_index"}:
            raise StateError("artifact-loss target selection has an unsupported format")
        targets.append(ArtifactLossTarget(item["outbox_id"], item["part_index"]))
    return tuple(targets)


def _inventory_document(args: argparse.Namespace) -> dict[str, object]:
    inventory = inspect_artifact_recovery_inventory(args.snapshot, args.spool_root)
    return {
        "ok": True,
        "schema_version": inventory.schema_version,
        "snapshot_sha256": inventory.snapshot_sha256,
        "ledger": {
            "sequence": inventory.ledger_sequence,
            "head": inventory.ledger_head,
            "valid": inventory.ledger_valid,
        },
        "coverage": inventory.coverage,
        "counts": inventory.counts,
        # Deliberately omit local spool paths.  This is an owner-local summary,
        # not a transport or deployment report.
        "entries": [
            {
                "outbox_id": entry.outbox_id,
                "part_index": entry.part_index,
                "availability": entry.availability,
                "file_size": entry.file_size,
                "file_sha256": entry.file_sha256,
            }
            for entry in inventory.entries
        ],
    }


def run(args: argparse.Namespace) -> int:
    if args.artifact_loss_command == "inventory":
        print(json.dumps(_inventory_document(args), ensure_ascii=False, indent=2))
        return 0
    targets = _load_targets(args.targets)
    if args.artifact_loss_command == "preview":
        with closing(HubState.open_read_only(args.state)) as state:
            preview = state.preview_artifact_loss(
                targets,
                spool_root=args.spool_root,
                recovery_audit_sha256=args.recovery_audit_sha256,
                operation_id=args.operation_id,
            )
        print(json.dumps({"ok": True, **asdict(preview)}, ensure_ascii=False, indent=2))
        return 0
    if args.artifact_loss_command == "apply":
        if not args.acknowledge_irrecoverable_loss:
            raise StateError("apply requires --acknowledge-irrecoverable-loss")
        with closing(HubState.open_existing(args.state)) as state:
            record = state.declare_artifact_loss(
                targets,
                spool_root=args.spool_root,
                recovery_audit_sha256=args.recovery_audit_sha256,
                expected_snapshot=args.snapshot,
                operation_id=args.operation_id,
                acknowledge_irrecoverable_loss=True,
            )
        print(json.dumps({"ok": True, **asdict(record)}, ensure_ascii=False, indent=2))
        return 0
    raise StateError("artifact-loss action is unavailable")
