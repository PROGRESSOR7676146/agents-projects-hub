"""Explicit local delivery consent, independent of config, transport or inference."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .state import HubState, StateError


def add_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser(
        "delivery-control", help="preview or explicitly reconcile one parked delivery wait locally"
    )
    command.add_argument("state", type=Path)
    command.add_argument("target_kind", choices=("final_outbox", "progress_delivery"))
    command.add_argument("target_id")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--snapshot")
    command.add_argument("--accept-unconfirmed-delivery", action="store_true")


def run(args: argparse.Namespace) -> int:
    if args.apply:
        if not args.snapshot or not args.accept_unconfirmed_delivery:
            raise StateError("apply requires --snapshot and --accept-unconfirmed-delivery")
        with closing(HubState.open_existing(args.state)) as state:
            disposition = state.reconcile_delivery_control(
                args.target_kind,
                args.target_id,
                expected_snapshot=args.snapshot,
                accept_unconfirmed_delivery=True,
            )
        result = dict(
            asdict(disposition), productive_replay_authorized=False, automatic_resend=False
        )
    else:
        if args.snapshot is not None or args.accept_unconfirmed_delivery:
            raise StateError("apply controls require --apply")
        with closing(HubState.open_read_only(args.state)) as state:
            result = asdict(state.preview_delivery_control(args.target_kind, args.target_id))
    print(json.dumps(result, indent=2))
    return 0
