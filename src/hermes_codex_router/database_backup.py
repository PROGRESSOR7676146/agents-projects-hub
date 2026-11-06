"""Shared SQLite-consistent backup mechanism; no migrations or runtime imports."""

from __future__ import annotations

import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_database(
    source: Path,
    destination: Path | None = None,
    *,
    timeout_seconds: float | None = None,
    read_only_source: bool = False,
) -> Path:
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("backup timeout must be finite and positive")
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination is None:
        destination = source.with_name(f"{source.name}.backup-{_timestamp()}")
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise FileExistsError(destination)
    connection_timeout = 5.0 if timeout_seconds is None else min(timeout_seconds, 0.1)
    source_connection = (
        sqlite3.connect(
            f"file:{quote(str(source), safe='/')}?mode=ro", uri=True, timeout=connection_timeout
        )
        if read_only_source
        else sqlite3.connect(source, timeout=connection_timeout)
    )
    destination_connection: sqlite3.Connection | None = None
    failed = False
    try:
        destination_connection = sqlite3.connect(destination, timeout=connection_timeout)
        if timeout_seconds is None:
            source_connection.backup(destination_connection)
        else:
            deadline = time.monotonic() + timeout_seconds

            def progress(status: int, remaining: int, total: int) -> None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("SQLite backup busy or exceeded its time budget")

            source_connection.backup(destination_connection, pages=256, progress=progress)
            destination_connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000
            )
        check = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise RuntimeError("SQLite backup integrity check failed")
    except BaseException:
        failed = True
        raise
    finally:
        if destination_connection is not None:
            try:
                destination_connection.close()
            except sqlite3.Error:
                pass
        try:
            source_connection.close()
        except sqlite3.Error:
            pass
        if failed and destination_connection is not None:
            # Only remove a destination we opened, and only after releasing handles.
            # Cleanup failure must not replace the original backup error.
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
    os.chmod(destination, 0o600)
    return destination
