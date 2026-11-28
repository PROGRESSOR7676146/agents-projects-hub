"""The installed extension of existing release-manifest and backup entries."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .installed_backup import backup_inputs, describe_installation, inspect_backup


def add_manifest_parsers(commands: Any) -> None:
    for name in ("installed", "inspect-backup"):
        parser = commands.add_parser(name)
        parser.add_argument("manifest", type=Path)
    parser = commands.add_parser("backup-inputs")
    parser.add_argument("primary", type=Path)
    parser.add_argument("secondary", type=Path, nargs="*")


def inspect_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.manifest_command == "backup-inputs":
        return backup_inputs(args.primary, args.secondary)
    if args.manifest_command == "inspect-backup":
        return inspect_backup(args.manifest)
    return describe_installation(args.manifest)
