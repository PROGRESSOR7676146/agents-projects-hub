"""Local read-only preview; no apply flags, config, credentials or runtime calls."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .state import HubState


def add_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser(
        "delivery-control", help="preview a parked delivery control target (apply unavailable)"
    )
    command.add_argument("state", type=Path)
    command.add_argument("target_kind", choices=("final_outbox", "progress_delivery"))
    command.add_argument("target_id")


def run(args: argparse.Namespace) -> int:
    with closing(HubState.open_read_only(args.state)) as state:
        preview = state.preview_delivery_control(args.target_kind, args.target_id)
    print(json.dumps(asdict(preview), indent=2))
    return 0
