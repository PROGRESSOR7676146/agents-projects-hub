"""Descriptor-bound immutable snapshots inside the private incoming spool."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path


class IncomingSnapshotError(ValueError):
    def __init__(self) -> None:
        super().__init__("Stored incoming material failed bounded snapshot validation.")


def read_verified_snapshot(
    root: Path,
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int,
) -> bytes:
    """Read one pinned regular file without following relative symlink components."""
    descriptors: list[int] = []
    try:
        if (
            not root.is_absolute()
            or not path.is_absolute()
            or type(max_bytes) is not int
            or max_bytes <= 0
            or type(expected_size) is not int
            or not 0 <= expected_size <= max_bytes
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise IncomingSnapshotError()
        relative = path.relative_to(root)
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise IncomingSnapshotError()
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptors.append(os.open(root, directory_flags))
        for component in relative.parts[:-1]:
            descriptors.append(os.open(component, directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=descriptors[-1],
        )
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise IncomingSnapshotError()
        data = bytearray()
        while len(data) <= expected_size:
            try:
                chunk = os.read(descriptor, min(65536, expected_size + 1 - len(data)))
            except InterruptedError:
                continue
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(data) != expected_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or hashlib.sha256(data).hexdigest() != expected_sha256
        ):
            raise IncomingSnapshotError()
        return bytes(data)
    except (OSError, ValueError, TypeError):
        raise IncomingSnapshotError() from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
