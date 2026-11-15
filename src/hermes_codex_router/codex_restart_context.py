"""Observe whether this process descends from an exact Codex socket owner.

This is a diagnostic only. No outcome authorizes a restart or proves idle work,
delivery, client quiescence, complete namespace visibility, or safe custody.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Literal

from .unix_listener_custody import process_identity, verify_listener

RestartContext = Literal["dependent", "not_observed", "unknown"]


def _parent_pid(proc_root: Path, pid: int) -> int:
    record = (proc_root / str(pid) / "stat").read_text()
    _, separator, fields = record.rpartition(") ")
    if not separator:
        raise ValueError("malformed process stat")
    values = fields.split()
    if len(values) < 2:
        raise ValueError("malformed process stat")
    return int(values[1])


def _ancestry_contains(proc_root: Path, owner_pid: int) -> bool:
    pid = os.getpid()
    visited: set[int] = set()
    while pid > 0:
        if pid in visited or len(visited) >= 256:
            raise ValueError("invalid process ancestry")
        visited.add(pid)
        before = process_identity(pid, proc_root=proc_root)
        parent = _parent_pid(proc_root, pid)
        if before != process_identity(pid, proc_root=proc_root):
            raise ValueError("process changed during ancestry inspection")
        if parent != _parent_pid(proc_root, pid):
            raise ValueError("process parent changed during ancestry inspection")
        if pid == owner_pid:
            return True
        if pid == 1 or parent == 0 or (parent == 1 and owner_pid != 1):
            return False
        pid = parent
    return False


def inspect(socket_path: Path, owner_pid: int) -> RestartContext:
    """Check a caller-supplied owner; absence of dependency is not safety proof."""
    try:
        if owner_pid <= 0 or not socket_path.is_absolute():
            return "unknown"
        resolved = socket_path.resolve(strict=True)
        owner = process_identity(owner_pid)
        if owner.uid != os.getuid():
            return "unknown"
        verify_listener(resolved, owner)
        found = _ancestry_contains(Path("/proc"), owner_pid)
        if process_identity(owner_pid) != owner:
            return "unknown"
        return "dependent" if found else "not_observed"
    except (OSError, ValueError, RuntimeError, OverflowError):
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket_path", type=Path)
    parser.add_argument("listener_pid", type=int)
    args = parser.parse_args()
    result = inspect(args.socket_path, args.listener_pid)
    print(result)
    return 2 if result == "unknown" else 0


if __name__ == "__main__":
    raise SystemExit(main())
