from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from .migrations import LATEST_SCHEMA_VERSION
from .sqlite_contention import is_sqlite_contention


def connect_existing(
    path: Path,
    *,
    writable: bool,
    state_error: type[Exception],
    contention_timeout_seconds: float | None = None,
) -> tuple[sqlite3.Connection, Path]:
    """Connect to an existing current-schema database; never create or migrate it.

    The schema is checked on the returned connection, so a file removed or
    replaced after an earlier probe is refused rather than created or migrated.
    """
    if contention_timeout_seconds is not None and (
        isinstance(contention_timeout_seconds, bool)
        or not isinstance(contention_timeout_seconds, (int, float))
        or not math.isfinite(contention_timeout_seconds)
        or not 0 < contention_timeout_seconds <= 5
    ):
        raise state_error("invalid state contention timeout")
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise state_error("state_unavailable")
        connection = sqlite3.connect(
            resolved.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
            uri=True,
            timeout=5.0 if contention_timeout_seconds is None else contention_timeout_seconds,
        )
        try:
            if not writable:
                connection.execute("PRAGMA query_only=ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != LATEST_SCHEMA_VERSION:
                raise state_error("state_schema_unsupported")
            connection.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            connection.close()
            raise
        return connection, resolved
    except state_error:
        raise
    except (OSError, sqlite3.Error) as error:
        if contention_timeout_seconds is not None and is_sqlite_contention(error):
            raise
        raise state_error("state_unavailable") from None
