"""Explicit local inspect/prepare/request/status, without implicit configuration."""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from pathlib import Path

from .worker_control_socket import exchange_control, inspect_control
from .worker_drain_protocol import (
    FRAME_LIMIT,
    ControlBinding,
    ControlError,
    decode_document,
    drain_request,
    status_request,
    validate_request,
)


def add_worker_parsers(commands: argparse._SubParsersAction) -> None:
    worker = commands.add_parser("worker", help="run one isolated provider queue worker")
    worker.add_argument("config", type=Path)
    worker.add_argument("--agent", default="codex")
    worker.add_argument("--slot", type=int, default=1)
    worker.add_argument("--poll-seconds", type=float, default=0.2)
    worker.add_argument(
        "--drain-control-socket", type=Path, help="opt-in private local control socket"
    )
    control = commands.add_parser("worker-control", help="explicit local operations-drain control")
    actions = control.add_subparsers(dest="control_command", required=True)
    inspect = actions.add_parser("inspect", help="discover only; no maintenance permission")
    inspect.add_argument("socket", type=Path)
    prepare = actions.add_parser("prepare", help="print one request; performs no control I/O")
    prepare.add_argument("binding_file", type=Path)
    prepare.add_argument("--request-id", required=True)
    prepare.add_argument("--ttl-seconds", type=int, default=30, choices=range(1, 61))
    for name in ("request", "status"):
        action = actions.add_parser(name)
        action.add_argument("socket", type=Path)
        action.add_argument("request_file", type=Path)


def _read_private(path: Path) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o777 != 0o600
            or info.st_nlink != 1
            or info.st_size > FRAME_LIMIT
        ):
            raise ControlError("unsafe_control_file")
        content = source.read(FRAME_LIMIT + 1)
        after = os.fstat(source.fileno())
        if any(
            getattr(after, field) != getattr(info, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_mode",
                "st_uid",
                "st_nlink",
            )
        ):
            raise ControlError("control_file_changed")
        return decode_document(content)


def worker_control_command(args: argparse.Namespace) -> int:
    try:
        if args.control_command == "inspect":
            result = inspect_control(args.socket)
        elif args.control_command == "prepare":
            binding = ControlBinding.parse(_read_private(args.binding_file).get("binding"))
            result = drain_request(
                binding,
                request_id=args.request_id,
                expires_ns=time.monotonic_ns() + args.ttl_seconds * 10**9,
            )
        else:
            request = _read_private(args.request_file)
            validate_request(request)
            if request["operation"] != "drain":
                raise ControlError("not_a_drain_request")
            binding = ControlBinding.parse(request["binding"])
            result = exchange_control(
                args.socket,
                binding,
                status_request(request) if args.control_command == "status" else request,
            )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"version": 1, "ok": False, "reason": "control_unconfirmed"}))
        return 2
