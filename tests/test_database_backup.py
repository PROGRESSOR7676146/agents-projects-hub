from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from hermes_codex_router.database_backup import backup_database


class DatabaseBackupTests(unittest.TestCase):
    def test_bounded_readonly_backup_handles_uri_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "state?example#.db"
            with closing(sqlite3.connect(source)) as con, con:
                con.execute("CREATE TABLE work (result TEXT)")
                con.execute("INSERT INTO work VALUES ('saved')")
            target = backup_database(
                source, Path(temp) / "snapshot.db", timeout_seconds=2, read_only_source=True
            )
            with closing(sqlite3.connect(target)) as con, con:
                self.assertEqual(con.execute("SELECT result FROM work").fetchall(), [("saved",)])
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_busy_copy_has_finite_budget_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "state.db"
            con = sqlite3.connect(source)
            con.execute("CREATE TABLE work (result TEXT)")
            con.commit()
            con.execute("BEGIN EXCLUSIVE")
            target = Path(temp) / "snapshot.db"
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(RuntimeError, "busy|time budget"):
                    backup_database(source, target, timeout_seconds=0.05, read_only_source=True)
            finally:
                con.rollback()
                con.close()
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(source.exists())
            self.assertFalse(target.exists())

    def test_nonfinite_or_nonpositive_deadline_refuses_before_io(self) -> None:
        for value in (float("nan"), float("inf"), 0, -1, True):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "finite and positive"),
            ):
                backup_database(Path("absent.db"), timeout_seconds=value)
