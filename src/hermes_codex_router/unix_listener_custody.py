"""Passive Linux stream-listener custody checks for explicit local maintenance."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .unix_socket_diag import query_listener, require_vfs


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


def _socket_identity(path: Path, uid: int) -> os.stat_result:
    info = path.lstat()
    if not path.is_absolute() or not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
        raise ValueError("unowned or non-socket filesystem path")
    return info


def _same_network_namespace(proc_root: Path, pid: int) -> None:
    if os.readlink(proc_root / "self/ns/net") != os.readlink(proc_root / str(pid) / "ns/net"):
        raise ValueError("different or unknown network namespace")


def verify_listener(
    path: Path, expected: ProcessIdentity, *, proc_root: Path = Path("/proc")
) -> int:
    """Verify exact listener FD and incarnation twice, without connecting or repair.

    Use the kernel pathname (resolve any approved alias separately). Binary
    SOCK_DIAG binds the listener to the current filesystem device/inode; text
    /proc names cannot establish custody. Artifact pins and directory/service
    policy remain the wrapper's gates. Different network namespaces refuse.
    Unavailable /proc evidence raises; it never establishes custody.
    """
    if process_identity(expected.pid, proc_root=proc_root) != expected:
        raise ValueError("process incarnation changed")
    _same_network_namespace(proc_root, expected.pid)
    before = _socket_identity(path, expected.uid)
    listener = query_listener(path)
    require_vfs(listener, before)
    inode = listener.kernel_inode
    _require_descriptor(proc_root, expected.pid, inode)
    if query_listener(path) != listener:
        raise ValueError("listener changed during observation")
    after = _socket_identity(path, expected.uid)
    require_vfs(listener, after)
    if (before.st_dev, before.st_ino, before.st_uid, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        after.st_mode,
    ):
        raise ValueError("filesystem socket changed during observation")
    _same_network_namespace(proc_root, expected.pid)
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
