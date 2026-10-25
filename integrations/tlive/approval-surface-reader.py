"""Offline child: parse a fictional native frame and read the real daemon IPC."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.approval_observations import approval_metadata
from hermes_codex_router.approval_surface import ApprovalWait, assess_surface, read_snapshot
from hermes_codex_router.codex_activity import CodexActivityEvent, normalize_codex_activity


def main() -> None:
    config = Path(sys.argv[1])
    path = Path(sys.argv[2])
    assert path == config.parent / "daemon.sock", "Hub and tlive socket paths differ"
    message = json.load(sys.stdin)
    params = message["params"]

    def wait_for(frame: dict) -> ApprovalWait:
        event = normalize_codex_activity(
            frame,
            expected_thread_id=params["threadId"],
            expected_turn_id=params["turnId"],
        )
        assert isinstance(event, CodexActivityEvent)
        assert type(event.request_id) is type(frame["id"])
        request_digest, item_digest = approval_metadata(event)
        return ApprovalWait(
            event.thread_id,
            event.turn_id,
            request_digest,
            item_digest,
            datetime.now(timezone.utc) - timedelta(seconds=60),
        )

    snapshot = read_snapshot(path)
    counts = assess_surface([wait_for(message)], snapshot, now=datetime.now(timezone.utc))
    print(json.dumps(counts), flush=True)
    assert counts == dict(waiting=1, grace=0, sent=1, unavailable=0, unknown=0), counts
    # A different JSON request-ID type must not inherit the positive receipt.
    other = dict(
        message, id=str(message["id"]) if type(message["id"]) is int else int(message["id"])
    )
    assert (
        assess_surface([wait_for(other)], snapshot, now=datetime.now(timezone.utc))["unknown"] == 1
    )
    for entry in (path.parent, path):
        info = entry.lstat()
        assert info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
        assert stat.S_ISDIR(info.st_mode) if entry == path.parent else stat.S_ISSOCK(info.st_mode)
    # Independently assert actual JSONL framing, beyond the production reader's
    # successful exchange. Node must keep servicing IPC while this child waits.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(b'{"kind":"approval.snapshot"}\n')
        data = b""
        while b"\n" not in data:
            chunk = client.recv(4096)
            assert chunk and len(data) + len(chunk) <= 131072
            data += chunk
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data)["kind"] == "approval.snapshot"


if __name__ == "__main__":
    main()
