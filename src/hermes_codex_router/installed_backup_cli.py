"""The installed extension of existing release-manifest and backup entries."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .installed_backup import describe_installation, inspect_backup


def add_manifest_parsers(commands: Any) -> None:
    for name in ("installed", "inspect-backup"):
        parser = commands.add_parser(name)
        parser.add_argument("manifest", type=Path)


def inspect_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.manifest_command == "inspect-backup":
        return inspect_backup(args.manifest)
    return describe_installation(args.manifest)
