"""Explicit local owner control, independent of configuration/Telegram/provider runtimes."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .state import HubState, StateError


def add_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser(
        "delivery-hold", help="inspect or release one unknown Telegram delivery hold locally"
    )
    command.add_argument("state", type=Path)
    command.add_argument("outbox_id")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--snapshot")
    command.add_argument("--continue-without-confirmed-delivery", action="store_true")


def run(args: argparse.Namespace) -> int:
    if args.apply:
        if not args.snapshot or not args.continue_without_confirmed_delivery:
            raise StateError("apply requires --snapshot and --continue-without-confirmed-delivery")
        with closing(HubState.open_existing(args.state)) as state:
            disposition = state.release_delivery_hold(
                args.outbox_id,
                expected_snapshot=args.snapshot,
                continue_without_confirmed_delivery=True,
            )
        result = dict(
            asdict(disposition),
            delivery_status="unknown",
            productive_replay_authorized=False,
            automatic_resend=False,
            effect=(
                "Previously authorized queued work may proceed, subject to independent safety boundaries."
                if disposition.hold_status == "released_by_owner"
                else "The recorded permission no longer matches its binding. The topic hold remains; inspect a fresh preview."
            ),
        )
    else:
        if args.snapshot is not None or args.continue_without_confirmed_delivery:
            raise StateError("apply controls require --apply")
        with closing(HubState.open_read_only(args.state)) as state:
            preview = state.preview_delivery_hold(args.outbox_id)
        result = dict(
            asdict(preview),
            delivery_status="unknown",
            action="continue_without_confirmed_delivery"
            if preview.hold_status == "outstanding"
            else None,
            effect=(
                "Releases only topic delivery/FIFO holds; saved evidence and session/writer controls remain."
                if preview.hold_status == "outstanding"
                else "Already released by a recorded owner decision; delivery remains unknown."
                if preview.hold_status == "released_by_owner"
                else "A recorded decision no longer matches this binding; the hold remains and no new decision can replace it."
            ),
        )
    print(json.dumps(result, indent=2))
    return 0
