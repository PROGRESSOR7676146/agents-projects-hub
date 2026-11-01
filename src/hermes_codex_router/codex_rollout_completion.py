"""Read-only completion evidence for the explicit external-server start gate.

Native JSONL is not a replay/import format. Unsupported or incomplete history
refuses start; no response, reasoning or tool content leaves this reader.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

MAX_ROLLOUT_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_TURNS = 4096


def completed_rollout_turns(home: Path, thread_id: str, project_root: str) -> frozenset[str]:
    """Require one private, unchanged native file with exact thread/root evidence."""
    try:
        return _read_completion(home, thread_id, project_root)
    except (OSError, ValueError, TypeError, RecursionError):
        return frozenset()


def _read_completion(home: Path, thread_id: str, project_root: str) -> frozenset[str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", thread_id):
        return frozenset()
    pattern = f"rollout-*-{thread_id}.jsonl"
    paths = list((home / "sessions").glob(f"*/*/*/{pattern}"))
    paths += list((home / "archived_sessions").glob(pattern))
    if len(paths) != 1 or paths[0].resolve() != paths[0].absolute():
        return frozenset()
    descriptor = os.open(paths[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > MAX_ROLLOUT_BYTES
        ):
            return frozenset()
        completed: set[str] = set()
        contexts: dict[str, str] = {}
        started: set[str] = set()
        active: str | None = None
        total = 0
        while line := stream.readline(MAX_RECORD_BYTES + 1):
            total += len(line)
            if (
                len(line) > MAX_RECORD_BYTES
                or total > MAX_ROLLOUT_BYTES
                or not line.endswith(b"\n")
            ):
                return frozenset()
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                return frozenset()
            payload = record["payload"]
            kind = record.get("type")
            if total == len(line) and kind != "session_meta":
                return frozenset()
            if kind == "session_meta":
                if payload.get("id") != thread_id or payload.get("cwd") != project_root:
                    return frozenset()
            elif kind == "turn_context":
                turn_id, cwd = payload.get("turn_id"), payload.get("cwd")
                if not isinstance(turn_id, str) or not isinstance(cwd, str):
                    return frozenset()
                if turn_id in contexts and contexts[turn_id] != cwd:
                    return frozenset()
                contexts[turn_id] = cwd
                if len(contexts) > MAX_TURNS:
                    return frozenset()
            elif kind == "event_msg" and payload.get("type") in (
                "task_started",
                "task_complete",
                "turn_aborted",
            ):
                turn_id = payload.get("turn_id")
                if not isinstance(turn_id, str) or not turn_id:
                    return frozenset()
                event = payload["type"]
                if event == "task_started":
                    if turn_id in started or active is not None:
                        return frozenset()
                    started.add(turn_id)
                    if len(started) > MAX_TURNS:
                        return frozenset()
                    active = turn_id
                else:
                    if active != turn_id:
                        return frozenset()
                    if event == "task_complete" and contexts.get(turn_id) == project_root:
                        completed.add(turn_id)
                    active = None
        after = os.fstat(stream.fileno())
        current = paths[0].lstat()
        if (
            active is not None
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        ):
            return frozenset()
        return frozenset(completed)
