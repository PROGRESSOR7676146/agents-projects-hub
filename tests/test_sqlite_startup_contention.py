from __future__ import annotations

import multiprocessing
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.state import HubState, StateError


def hold_writer(path: str, control: Connection) -> None:
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE topics SET title='Uncommitted'")
        control.send("locked")
        if not control.poll(10):
            raise TimeoutError("Fictional writer was not released")
        control.recv()
        writer.rollback()
    control.close()


class SqliteStartupContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example",
            execution_root=self.root,
        )
        self.state._connection.execute("PRAGMA busy_timeout=10")

    def test_current_scopes_need_no_writer_lock_during_another_process_write(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        writer = context.Process(target=hold_writer, args=(str(self.path), child))
        writer.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10), "Fictional writer did not acquire its lock")
            self.assertEqual(parent.recv(), "locked")
            self.assertEqual(
                self.state.reconcile_legacy_execution_scopes({"example-project": self.root}), 0
            )
            self.assertEqual(self.state.get_topic(self.topic.topic_id).title, "Example")
            self.assertFalse(self.state._connection.in_transaction)
        finally:
            if writer.is_alive():
                parent.send("release")
            parent.close()
            writer.join(5)
            if writer.is_alive():
                writer.kill()
                writer.join(5)
            writer.close()

    def test_legacy_scope_still_requires_writer_and_reconciles_after_release(self) -> None:
        with self.state._connection:
            self.state._connection.execute("UPDATE topics SET execution_scope=''")
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError) as raised:
                self.state.reconcile_legacy_execution_scopes({"example-project": self.root})
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            self.assertFalse(self.state._connection.in_transaction)
            self.assertEqual(self.state.get_topic(self.topic.topic_id).execution_scope, "")
            writer.rollback()
        self.assertEqual(
            self.state.reconcile_legacy_execution_scopes({"example-project": self.root}), 1
        )
        self.assertEqual(
            self.state.get_topic(self.topic.topic_id).execution_scope, "root:" + str(self.root)
        )

    def test_current_scope_does_not_skip_mismatched_active_lane(self) -> None:
        lane = self.root / "lane"
        lane.mkdir()
        self.state.register_lane(
            lane_id="example-lane",
            project_id="example-project",
            worktree_path=lane,
            branch_name="example-lane",
        )
        self.state.bind_lane("example-lane", self.topic.topic_id)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope=?", ("root:" + str(self.root),)
            )
        with self.assertRaisesRegex(StateError, "active lane execution scope mismatch"):
            self.state.reconcile_legacy_execution_scopes({"example-project": self.root})
        self.assertFalse(self.state._connection.in_transaction)

    def test_noop_reconciliation_does_not_commit_a_callers_transaction(self) -> None:
        self.state._connection.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaisesRegex(StateError, "cannot nest an immediate state transaction"):
                self.state.reconcile_legacy_execution_scopes({"example-project": self.root})
            self.assertTrue(self.state._connection.in_transaction)
        finally:
            self.state._connection.rollback()

    def test_candidate_is_rechecked_after_another_connection_changes_it(self) -> None:
        lane = self.root / "lane"
        lane.mkdir()
        self.state.register_lane(
            lane_id="example-lane",
            project_id="example-project",
            worktree_path=lane,
            branch_name="example-lane",
        )
        self.state.bind_lane("example-lane", self.topic.topic_id)
        legacy = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=78,
            title="Legacy",
        )
        check = self.state._lane_state.needs_scope_reconciliation

        def change_after_check() -> bool:
            needed = check()
            with closing(sqlite3.connect(self.path)) as other, other:
                other.execute(
                    "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                    ("root:" + str(self.root), self.topic.topic_id),
                )
            return needed

        with patch.object(
            self.state._lane_state, "needs_scope_reconciliation", side_effect=change_after_check
        ):
            with self.assertRaisesRegex(StateError, "active lane execution scope mismatch"):
                self.state.reconcile_legacy_execution_scopes({"example-project": self.root})
        self.assertEqual(
            self.state.get_topic(legacy.topic_id).execution_scope, "project:example-project"
        )
        self.assertFalse(self.state._connection.in_transaction)

    def _fill_runtime_journal(self, now: datetime) -> None:
        with self.state._connection:
            self.state._connection.executemany(
                """INSERT INTO runtime_events(component, level, code, detail, created_at)
                   VALUES ('controller', 'info', 'retained', 'Example', ?)""",
                [(now.isoformat(),)] * 10_000,
            )

    def test_full_runtime_journal_prune_has_bounded_sql_work(self) -> None:
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self._fill_runtime_journal(now)
        instructions = 0

        def interrupt_excessive_write_work() -> int:
            nonlocal instructions
            instructions += 1000
            return int(instructions > 80_000)

        # Bound work while holding SQLite's sole writer without wall-clock
        # assertions. Rechecking every retained row exceeded this budget.
        self.state._connection.set_progress_handler(interrupt_excessive_write_work, 1000)
        try:
            self.state.record_runtime_event(
                "controller", "info", "newest", "Example", recorded_at=now
            )
        finally:
            self.state._connection.set_progress_handler(None, 0)
        self.assertFalse(self.state._connection.in_transaction)
        self.assertEqual(
            self.state._connection.execute("SELECT count(*) FROM runtime_events").fetchone()[0],
            10_000,
        )
        self.assertIsNotNone(self.state.latest_runtime_event("controller", "newest"))

    def test_backdated_event_does_not_displace_newer_retained_events(self) -> None:
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self._fill_runtime_journal(now)
        retained = self.state._connection.execute(
            "SELECT event_id FROM runtime_events ORDER BY event_id"
        ).fetchall()
        self.state.record_runtime_event(
            "controller", "info", "older", "Example", recorded_at=now - timedelta(seconds=1)
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT event_id FROM runtime_events ORDER BY event_id"
            ).fetchall(),
            retained,
        )
        self.assertIsNone(self.state.latest_runtime_event("controller", "older"))


if __name__ == "__main__":
    unittest.main()
