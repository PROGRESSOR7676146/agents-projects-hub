from __future__ import annotations

import fcntl
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WorkerProcessLockError(RuntimeError):
    pass


@contextmanager
def worker_process_lock(state_path: Path, worker_id: str) -> Iterator[None]:
    """Keep two CLI processes from claiming the same worker identity."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", worker_id) is None:
        raise WorkerProcessLockError("invalid worker identity")
    path = state_path.with_name(f"{state_path.name}.{worker_id}.lock")
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WorkerProcessLockError("worker lock file is not private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerProcessLockError(f"worker slot {worker_id} is already running") from None
        yield
    finally:
        os.close(descriptor)
