"""SQLite's retryable lock codes; exception text is never an authority."""

from __future__ import annotations

import sqlite3


def is_sqlite_contention(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return (
        isinstance(error, sqlite3.OperationalError)
        and isinstance(code, int)
        and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    )
