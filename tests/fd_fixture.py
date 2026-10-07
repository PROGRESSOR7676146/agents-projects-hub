"""Check calling-thread descriptor tracking; cleanup always belongs to callers."""

from __future__ import annotations

import os
import stat
import threading
import unittest
import warnings
from contextlib import ExitStack, contextmanager
from typing import Iterator
from unittest.mock import patch

_FSTAT = os.fstat
_LISTDIR = os.listdir


def _identity(fd: int) -> tuple[int, int, int] | None:
    try:
        info = _FSTAT(fd)
    except OSError:
        return None
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _snapshot() -> set[tuple[int, tuple[int, int, int]]]:
    return {
        (fd, identity)
        for name in _LISTDIR("/proc/self/fd")
        if (identity := _identity(fd := int(name))) is not None
    }


@contextmanager
def assert_descriptor_cleanup(
    case: unittest.TestCase,
    *,
    forbid_preexisting_close: bool = False,
    forbid_unattributed: bool = False,
) -> Iterator[None]:
    """Check allocations; strict mode also rejects intercepted foreign closes.

    Only calling-thread os.close/dup2 calls are attributed by the optional check;
    C-level FileIO/socket finalizers remain outside interception. No cleanup
    authority is inferred from either descriptor numbers or snapshots. Strict
    closure matching uses entry identity; unobserved same-inode reuse remains
    ambiguous and is conservatively rejected. Identity reads use unpatched OS
    functions so a test's fstat mock cannot demote a tracked leak to uncertainty.
    """
    before = _snapshot()
    preexisting = dict(before)
    closed_preexisting: set[int] = set()
    tracked: dict[int, tuple[int, int, int] | None] = {}
    foreign: dict[int, tuple[int, int, int] | None] = {}
    caller = threading.get_ident()
    original_open, original_dup, original_dup2, original_close = (
        os.open,
        os.dup,
        os.dup2,
        os.close,
    )
    original_pipe = os.pipe
    original_memfd = getattr(os, "memfd_create", None)

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

    def foreign_close(fd: int) -> bool:
        return (
            forbid_preexisting_close
            and threading.get_ident() == caller
            and fd in preexisting
            and fd not in tracked
            and fd not in foreign
            and _identity(fd) == preexisting[fd]
        )

    def track_dup2(fd: int, target: int, inheritable: bool = True) -> int:
        closes_preexisting = fd != target and foreign_close(target)
        result = original_dup2(fd, target, inheritable=inheritable)
        if fd == target:
            return result
        if closes_preexisting:
            closed_preexisting.add(target)
        return record(result)

    def track_pipe() -> tuple[int, int]:
        read_fd, write_fd = original_pipe()
        return record(read_fd), record(write_fd)

    def track_memfd(*args: object, **kwargs: object) -> int:
        assert original_memfd is not None
        return record(original_memfd(*args, **kwargs))

    def track_close(fd: int) -> None:
        closes_preexisting = foreign_close(fd)
        original_close(fd)
        if closes_preexisting:
            closed_preexisting.add(fd)
        # Observe successful close/overwrite events across all threads.
        tracked.pop(fd, None)
        foreign.pop(fd, None)

    primary: BaseException | None = None
    try:
        with ExitStack() as patches:
            patches.enter_context(patch("os.open", side_effect=track_open))
            patches.enter_context(patch("os.dup", side_effect=track_dup))
            patches.enter_context(patch("os.dup2", side_effect=track_dup2))
            patches.enter_context(patch("os.close", side_effect=track_close))
            patches.enter_context(patch("os.pipe", side_effect=track_pipe))
            if original_memfd is not None:
                patches.enter_context(patch("os.memfd_create", side_effect=track_memfd))
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
            f"; preexisting descriptors closed by calling-thread os.close: {sorted(closed_preexisting)}"
        )
        if primary is not None:
            if remaining or unattributed or closed_preexisting:
                primary.add_note(detail)
        else:
            if unattributed:
                warnings.warn(detail, ResourceWarning, stacklevel=2)
            case.assertFalse(remaining, detail)
            case.assertFalse(closed_preexisting, detail)
            if forbid_unattributed:
                case.assertFalse(unattributed, detail)
        # Never close here: neither interception nor snapshots prove current
        # cleanup authority. Regression tests explicitly clean their own FDs.
