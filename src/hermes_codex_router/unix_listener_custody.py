"""Passive Linux stream-listener custody checks for explicit local maintenance."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    boot_id: str
    start_ticks: int
    executable: str
    cgroup: str
    uid: int


def process_identity(pid: int, *, proc_root: Path = Path("/proc")) -> ProcessIdentity:
    """Capture host identity; the caller must compare it with reviewed service pins."""
    if pid <= 0:
        raise ValueError("invalid process identity")
    root = proc_root / str(pid)
    return ProcessIdentity(
        pid,
        (proc_root / "sys/kernel/random/boot_id").read_text().strip(),
        int((root / "stat").read_text().rsplit(")", 1)[1].split()[19]),
        os.readlink(root / "exe"),
        (root / "cgroup").read_text().strip(),
        root.stat().st_uid,
    )


def listener_inode(rows: str, path: Path) -> int:
    """Accepted connections may share the path; only SO_ACCEPTCON rows qualify."""
    matches = []
    for row in rows.splitlines():
        fields = row.split(maxsplit=7)
        if len(fields) != 8 or fields[7] != str(path):
            continue
        if int(fields[3], 16) & 0x10000 and fields[4] == "0001" and fields[5] == "01":
            matches.append(int(fields[6]))
    if len(matches) != 1 or matches[0] <= 0:
        raise ValueError("missing or ambiguous Unix stream listener")
    return matches[0]


def verify_listener(
    path: Path, expected: ProcessIdentity, *, proc_root: Path = Path("/proc")
) -> int:
    """Verify exact listener FD and incarnation twice, without connecting or repair.

    Use the kernel pathname (resolve any approved alias separately). Filesystem
    custody, artifact hashes and service policy remain the wrapper's gates.
    Unavailable /proc evidence raises; it never establishes custody.
    """
    if process_identity(expected.pid, proc_root=proc_root) != expected:
        raise ValueError("process incarnation changed")
    table = proc_root / "net/unix"
    inode = listener_inode(table.read_text(), path)
    _require_descriptor(proc_root, expected.pid, inode)
    if listener_inode(table.read_text(), path) != inode:
        raise ValueError("listener changed during observation")
    if process_identity(expected.pid, proc_root=proc_root) != expected:
        raise ValueError("process incarnation changed during observation")
    _require_descriptor(proc_root, expected.pid, inode)
    if process_identity(expected.pid, proc_root=proc_root) != expected:
        raise ValueError("process incarnation changed after descriptor observation")
    return inode


def _require_descriptor(proc_root: Path, pid: int, inode: int) -> None:
    owned = False
    for descriptor in (proc_root / str(pid) / "fd").iterdir():
        try:
            owned |= os.readlink(descriptor) == f"socket:[{inode}]"
        except FileNotFoundError:
            continue  # Unrelated descriptors may close while being observed.
    if not owned:
        raise ValueError("listener descriptor not owned by expected process")
