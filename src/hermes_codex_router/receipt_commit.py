"""Bounded local-only persistence retry. This helper never owns transport I/O."""

from __future__ import annotations

import sqlite3
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def commit_receipt(commit: Callable[[], T]) -> T:
    for attempt in range(3):
        try:
            return commit()
        except (sqlite3.OperationalError, OSError):
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable receipt retry")
