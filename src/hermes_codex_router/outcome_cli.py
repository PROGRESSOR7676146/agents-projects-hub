"""Explicit local diagnostic command; never credentials, providers or state writes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .hub_config import load_external_worker_config
from .state import HubState, StateError


def add_outcome_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser(
        "outcome-journal", help="read one exact job's saved outcome evidence"
    )
    command.add_argument("config", type=Path)
    command.add_argument("job_id")


def _emit(encoded: str, code: int) -> int:
    try:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        # A partial or closed output cannot carry a second error document.
        return 2
    return code


def outcome_command(args: argparse.Namespace) -> int:
    try:
        config = load_external_worker_config(args.config)
    except Exception:
        return _emit(json.dumps({"ok": False, "error": "outcome_config_unavailable"}), 2)
    try:
        state = HubState.open_read_only(config.state_path)
        try:
            result = state.provider_job_outcome(args.job_id)
            encoded = json.dumps({"ok": True, **result.as_dict()}, indent=2)
        finally:
            state.close()
    except Exception as error:
        code = str(error) if isinstance(error, StateError) else "outcome_projection_unavailable"
        if code not in {
            "outcome_job_id_invalid",
            "outcome_job_not_found",
            "outcome_projection_unavailable",
            "state_unavailable",
            "state_schema_unsupported",
        }:
            code = "outcome_projection_unavailable"
        return _emit(json.dumps({"ok": False, "error": code}), 2)
    return _emit(encoded, 0)
