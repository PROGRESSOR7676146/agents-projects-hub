from __future__ import annotations

import sqlite3
from pathlib import Path

from .migrations import LATEST_SCHEMA_VERSION


def connect_existing(
    path: Path,
    *,
    writable: bool,
    state_error: type[Exception],
) -> tuple[sqlite3.Connection, Path]:
    """Connect to an existing current-schema database; never create or migrate it.

    The schema is checked on the returned connection, so a file removed or
    replaced after an earlier probe is refused rather than created or migrated.
    """
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise state_error("state_unavailable")
        connection = sqlite3.connect(
            resolved.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
            uri=True,
            timeout=5.0,
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
    except (OSError, sqlite3.Error):
        raise state_error("state_unavailable") from None
