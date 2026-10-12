"""Prepare the stable bind source for Codex's private daemon socket.

The directory must exist before systemd constructs a worker's PrivateTmp mount.
A missing optional BindPaths source is silently skipped at that point and cannot
be repaired inside the already running worker namespace.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def ensure_codex_daemon_directory(*, base: Path = Path("/tmp")) -> Path:
    path = base / f"codex-daemon-{os.getuid()}"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o777 != 0o700
    ):
        raise ValueError("Codex daemon socket directory is not a private owned directory")
    return path


def main() -> int:
    ensure_codex_daemon_directory()
    return 0
