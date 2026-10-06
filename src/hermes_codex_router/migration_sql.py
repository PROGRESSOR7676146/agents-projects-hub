"""Trusted SQL script execution that preserves its caller's transaction."""

from __future__ import annotations

import sqlite3


def _execute_migration_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute one trusted migration script without sqlite3's implicit COMMIT.

    ``Connection.executescript`` commits an open transaction before executing
    its input.  Migrations must instead remain inside the surrounding
    ``BEGIN IMMEDIATE`` so a DDL or retention fault restores the exact
    pre-migration database, including writes which committed after the backup
    snapshot was taken.
    """
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        statement = "".join(pending)
        if not sqlite3.complete_statement(statement):
            continue
        if statement.strip():
            connection.execute(statement)
        pending.clear()
    if "".join(pending).strip():
        raise RuntimeError("incomplete SQLite migration statement")
