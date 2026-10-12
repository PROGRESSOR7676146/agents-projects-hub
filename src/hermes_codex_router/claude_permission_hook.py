"""Standalone Claude PermissionRequest hook; the worker owns every decision."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Mapping
from typing import Any, BinaryIO, TextIO
from uuid import uuid4

from .claude_permission_protocol import (
    PermissionProtocolError,
    canonical_uuid,
    event_digest,
    parse_json_strict,
)

_TOOLS = frozenset({"Read", "Glob", "Grep", "Write", "Edit"})
_DENY = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny", "message": "Permission denied."},
    }
}


def _event(raw: bytes) -> dict[str, Any]:
    value = parse_json_strict(raw, max_bytes=65536)
    if not isinstance(value, dict) or value.get("hook_event_name") != "PermissionRequest":
        raise PermissionProtocolError("invalid hook event")
    canonical_uuid(value.get("session_id"))
    cwd = value.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\x00" in cwd:
        raise PermissionProtocolError("invalid hook directory")
    if value.get("tool_name") not in _TOOLS or not isinstance(value.get("tool_input"), dict):
        raise PermissionProtocolError("invalid hook tool")
    if any(
        key in value
        for key in (
            "agent_id",
            "agentId",
            "parent_tool_use_id",
            "permission_updates",
            "updatedPermissions",
        )
    ):
        raise PermissionProtocolError("unsupported hook authority")
    return value


def _broker(socket_path: str, event: dict[str, Any]) -> bool:
    if not os.path.isabs(socket_path) or "\x00" in socket_path:
        return False
    nonce = str(uuid4())
    digest = event_digest(event)
    request = {
        "kind": "claude.permission.request",
        "version": 1,
        "nonce": nonce,
        "event": event,
        "eventDigest": digest,
    }
    wire = (
        json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    if len(wire) > 131072:
        return False
    deadline = time.monotonic() + 600.0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            sock.connect(socket_path)
            sock.sendall(wire)
            buf = bytearray()
            while time.monotonic() < deadline:
                sock.settimeout(min(0.2, max(0.01, deadline - time.monotonic())))
                try:
                    chunk = sock.recv(min(4096, 131073 - len(buf)))
                except socket.timeout:
                    continue
                if not chunk:
                    if not buf or buf.count(b"\n") != 1 or not buf.endswith(b"\n"):
                        return False
                    response = parse_json_strict(bytes(buf[:-1]))
                    return (
                        isinstance(response, dict)
                        and set(response) == {"kind", "version", "nonce", "eventDigest", "decision"}
                        and response["kind"] == "claude.permission.result"
                        and type(response["version"]) is int
                        and response["version"] == 1
                        and response["nonce"] == nonce
                        and response["eventDigest"] == digest
                        and response["decision"] == "allow"
                    )
                buf.extend(chunk)
                if len(buf) > 131072:
                    return False
                if buf.count(b"\n") > 1 or (b"\n" in buf and not buf.endswith(b"\n")):
                    return False
    except (OSError, ValueError, PermissionProtocolError):
        return False
    return False


def run_hook(stdin: BinaryIO, stdout: TextIO, environ: Mapping[str, str]) -> int:
    """Always emit one official hook decision and exit successfully."""
    allowed = False
    try:
        raw = stdin.read(65537)
        if len(raw) <= 65536:
            event = _event(raw)
            path = environ.get("HUB_CLAUDE_PERMISSION_SOCKET", "")
            if path:
                allowed = _broker(path, event)
    except (OSError, ValueError, TypeError, PermissionProtocolError):
        pass
    output = (
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }
        if allowed
        else _DENY
    )
    stdout.write(json.dumps(output, separators=(",", ":"), ensure_ascii=False) + "\n")
    stdout.flush()
    return 0


def main() -> int:
    return run_hook(sys.stdin.buffer, sys.stdout, os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
