"""Check owned descriptor cleanup while tolerating unrelated resource collection."""

from __future__ import annotations

import os
import stat
import unittest
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
    owned: dict[int, tuple[int, int, int] | None] = {}
    original_open, original_dup, original_close = os.open, os.dup, os.close

    def track_open(*args: object, **kwargs: object) -> int:
        fd = original_open(*args, **kwargs)  # type: ignore[arg-type]
        owned[fd] = _identity(fd)
        return fd

    def track_dup(fd: int) -> int:
        copied = original_dup(fd)
        owned[copied] = _identity(copied)
        return copied

    def track_close(fd: int) -> None:
        original_close(fd)
        owned.pop(fd, None)

    primary: BaseException | None = None
    try:
        with (
            patch("os.open", side_effect=track_open),
            patch("os.dup", side_effect=track_dup),
            patch("os.close", side_effect=track_close),
        ):
            yield
    except BaseException as error:
        primary = error
        raise
    finally:
        # Track ownership through closes/reuse. The identity snapshot also
        # catches APIs such as pipe/memfd allocation, ignoring unrelated closes.
        owned_leaks = {
            fd
            for fd, identity in owned.items()
            if identity is not None and identity == _identity(fd)
        }
        unattributed = {fd for fd, _ in _snapshot() - before} - owned_leaks
        detail = f"owned descriptors remain open: {sorted(owned_leaks)}; unattributed new descriptors: {sorted(unattributed)}"
        try:
            if primary is not None:
                if owned_leaks or unattributed:
                    primary.add_note(detail)
            else:
                case.assertFalse(owned_leaks or unattributed, detail)
        finally:
            # Snapshot differences alone do not prove cleanup ownership.
            for fd in owned_leaks:
                if _identity(fd) != owned[fd]:
                    continue
                try:
                    original_close(fd)
                except OSError:
                    pass
