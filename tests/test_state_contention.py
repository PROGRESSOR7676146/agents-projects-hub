from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from hermes_codex_router.state import HubState, StateError


class StateContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
        )
        self.state._connection.execute("PRAGMA journal_mode=DELETE")
        self.state._connection.execute("PRAGMA busy_timeout=10")

    def test_real_commit_busy_rolls_back_and_connection_can_begin_again(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=0.01)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT title FROM topics").fetchall()
            with self.assertRaises(sqlite3.OperationalError) as raised:
                with self.state._immediate_transaction():
                    self.state._connection.execute("UPDATE topics SET title='Uncommitted'")
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            self.assertFalse(self.state._connection.in_transaction)
            self.assertEqual(reader.execute("SELECT title FROM topics").fetchone()[0], "Example")
            reader.rollback()
        with self.state._immediate_transaction():
            self.state._connection.execute("UPDATE topics SET title='Committed'")
        self.assertEqual(self.state.get_topic(self.topic.topic_id).title, "Committed")
        self.assertFalse(self.state._connection.in_transaction)

    def test_deferred_integrity_error_at_commit_also_rolls_back(self) -> None:
        self.state._connection.executescript(
            "CREATE TABLE example_parent(id INTEGER PRIMARY KEY);"
            "CREATE TABLE example_child(parent_id INTEGER REFERENCES example_parent(id) "
            "DEFERRABLE INITIALLY DEFERRED);"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.state._immediate_transaction():
                self.state._connection.execute("INSERT INTO example_child VALUES(1)")
        self.assertFalse(self.state._connection.in_transaction)
        self.assertEqual(
            self.state._connection.execute("SELECT count(*) FROM example_child").fetchone()[0], 0
        )

    def test_control_open_preserves_real_busy_from_schema_probe(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=0.01)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(sqlite3.OperationalError) as raised:
                HubState.open_existing(
                    self.path, codex_permission_profile=None, contention_timeout_seconds=0.01
                )
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            writer.rollback()
        with closing(
            HubState.open_existing(
                self.path, codex_permission_profile=None, contention_timeout_seconds=0.01
            )
        ) as control:
            self.assertEqual(control._connection.execute("PRAGMA busy_timeout").fetchone()[0], 10)
            self.assertEqual(control.get_topic(self.topic.topic_id).title, "Example")

    def test_invalid_control_timeouts_and_missing_state_fail_without_creation(self) -> None:
        for timeout in (0, -1, 6, True, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(StateError):
                HubState.open_existing(
                    self.path, codex_permission_profile=None, contention_timeout_seconds=timeout
                )
        missing = self.path.with_name("example-missing.db")
        with self.assertRaises(StateError):
            HubState.open_existing(
                missing, codex_permission_profile=None, contention_timeout_seconds=0.01
            )
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
