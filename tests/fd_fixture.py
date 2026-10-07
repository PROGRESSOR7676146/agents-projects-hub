"""Check calling-thread descriptor tracking; cleanup always belongs to callers."""

from __future__ import annotations

import os
import stat
import threading
import unittest
import warnings
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch


def _identity(fd: int) -> tuple[int, int, int] | None:
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _snapshot() -> set[tuple[int, tuple[int, int, int]]]:
    return {
        (fd, identity)
        for name in os.listdir("/proc/self/fd")
        if (identity := _identity(fd := int(name))) is not None
    }


@contextmanager
def assert_descriptor_cleanup(case: unittest.TestCase) -> Iterator[None]:
    before = _snapshot()
    tracked: dict[int, tuple[int, int, int] | None] = {}
    foreign: dict[int, tuple[int, int, int] | None] = {}
    caller = threading.get_ident()
    original_open, original_dup, original_dup2, original_close = (
        os.open,
        os.dup,
        os.dup2,
        os.close,
    )

    def record(fd: int) -> int:
        tracked.pop(fd, None)
        foreign.pop(fd, None)
        target = tracked if threading.get_ident() == caller else foreign
        target[fd] = _identity(fd)
        return fd

    def track_open(*args: object, **kwargs: object) -> int:
        fd = original_open(*args, **kwargs)  # type: ignore[arg-type]
        return record(fd)

    def track_dup(fd: int) -> int:
        return record(original_dup(fd))

    def track_dup2(fd: int, target: int, inheritable: bool = True) -> int:
        return record(original_dup2(fd, target, inheritable=inheritable))

    def track_close(fd: int) -> None:
        original_close(fd)
        # Observe successful close/overwrite events across all threads.
        tracked.pop(fd, None)
        foreign.pop(fd, None)

    primary: BaseException | None = None
    try:
        with (
            patch("os.open", side_effect=track_open),
            patch("os.dup", side_effect=track_dup),
            patch("os.dup2", side_effect=track_dup2),
            patch("os.close", side_effect=track_close),
        ):
            yield
    except BaseException as error:
        primary = error
        raise
    finally:
        # Identity checks can detect tracked descriptors remaining open, but
        # cannot establish ownership after an unobserved same-inode reuse.
        remaining = {
            fd
            for fd, identity in tuple(tracked.items())
            if identity is not None and identity == _identity(fd)
        }
        unrelated = {fd for fd, identity in tuple(foreign.items()) if identity == _identity(fd)}
        unattributed = {fd for fd, _ in _snapshot() - before} - remaining - unrelated
        detail = (
            f"tracked calling-thread descriptors remain open: {sorted(remaining)}; "
            f"unattributed new descriptors (ownership unknown): {sorted(unattributed)}"
        )
        if primary is not None:
            if remaining or unattributed:
                primary.add_note(detail)
        else:
            if unattributed:
                warnings.warn(detail, ResourceWarning, stacklevel=2)
            case.assertFalse(remaining, detail)
        # Never close here: neither interception nor snapshots prove current
        # cleanup authority. Regression tests explicitly clean their own FDs.
