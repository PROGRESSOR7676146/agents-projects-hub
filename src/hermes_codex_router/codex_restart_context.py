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
    expected_parent = None
    while pid > 0:
        if pid in visited or len(visited) >= 256:
            raise ValueError("invalid process ancestry")
        visited.add(pid)
        before = process_identity(pid, proc_root=proc_root)
        if expected_parent is not None and before != expected_parent:
            raise ValueError("parent incarnation changed during ancestry inspection")
        parent = _parent_pid(proc_root, pid)
        if before != process_identity(pid, proc_root=proc_root):
            raise ValueError("process changed during ancestry inspection")
        if parent != _parent_pid(proc_root, pid):
            raise ValueError("process parent changed during ancestry inspection")
        if pid == owner_pid:
            return True
        if pid == 1 or parent == 0 or (parent == 1 and owner_pid != 1):
            return False
        parent_identity = process_identity(parent, proc_root=proc_root)
        if parent_identity.start_ticks > before.start_ticks:
            raise ValueError("parent is newer than child")
        if process_identity(pid, proc_root=proc_root) != before:
            raise ValueError("child incarnation changed during ancestry inspection")
        if _parent_pid(proc_root, pid) != parent:
            raise ValueError("process parent changed during ancestry inspection")
        expected_parent = parent_identity
        pid = parent
    return False


def _require_matching_pid_namespace(proc_root: Path, owner_pid: int) -> None:
    """Refuse if procfs numbers do not identify this process and its peer."""
    if int(os.readlink(proc_root / "self")) != os.getpid():
        raise ValueError("procfs PID numbering differs from this process")
    if os.readlink(proc_root / "self/ns/pid") != os.readlink(proc_root / str(owner_pid) / "ns/pid"):
        raise ValueError("listener PID namespace differs")


def inspect(socket_path: Path, owner_pid: int) -> RestartContext:
    """Check a caller-supplied owner; absence of dependency is not safety proof."""
    try:
        if owner_pid <= 0 or not socket_path.is_absolute():
            return "unknown"
        _require_matching_pid_namespace(Path("/proc"), owner_pid)
        resolved = socket_path.resolve(strict=True)
        owner = process_identity(owner_pid)
        if owner.uid != os.getuid():
            return "unknown"
        verify_listener(resolved, owner)
        found = _ancestry_contains(Path("/proc"), owner_pid)
        verify_listener(resolved, owner)
        if process_identity(owner_pid) != owner:
            return "unknown"
        return "dependent" if found else "not_observed"
    except (OSError, ValueError, RuntimeError, OverflowError, IndexError):
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
