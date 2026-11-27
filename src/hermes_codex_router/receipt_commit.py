"""Bounded local-only persistence retry. This helper never owns transport I/O."""

from __future__ import annotations

import sqlite3
import time
from typing import Callable, Literal, TypeVar

from .diagnostic_log import receipt_contention

T = TypeVar("T")


def commit_receipt(commit: Callable[[], T], *, operation: Literal["final", "progress"]) -> T:
    for attempt in range(3):
        started = time.monotonic()
        try:
            return commit()
        except (sqlite3.OperationalError, OSError) as error:
            receipt_contention(operation, error, time.monotonic() - started, attempt + 1)
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable receipt retry")
