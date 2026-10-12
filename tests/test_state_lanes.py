from __future__ import annotations

import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from hermes_codex_router.state import HubState, StateError


class LaneStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Fictional lane topic",
            execution_root=self.root,
        )
        self.lane = self.root / "lane"
        self.lane.mkdir()

    def register(self, *, topic_id: int | None = None) -> None:
        self.state.register_lane(
            lane_id="fictional-lane",
            project_id="example-project",
            worktree_path=self.lane,
            branch_name="lane/fictional-lane",
            topic_id=topic_id,
        )

    def test_register_with_binding_fault_rolls_back_entire_new_lane(self) -> None:
        self.state._connection.execute(
            """CREATE TRIGGER fictional_bind_fault BEFORE UPDATE OF execution_scope ON topics
               BEGIN SELECT RAISE(ABORT, 'fictional bind fault'); END"""
        )
        before = tuple(self.state._connection.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fictional bind fault"):
            self.register(topic_id=self.topic.topic_id)
        self.assertEqual(tuple(self.state._connection.iterdump()), before)
        self.assertFalse(self.state._connection.in_transaction)

    def test_cleanup_commit_busy_rolls_back_both_timestamps_and_can_retry(self) -> None:
        self.register()
        self.state.archive_lane("fictional-lane")
        before = self.state.get_lane("fictional-lane")
        self.state._connection.execute("PRAGMA journal_mode=DELETE")
        self.state._connection.execute("PRAGMA busy_timeout=10")
        with closing(sqlite3.connect(self.path, timeout=0.01)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM worktree_lanes").fetchall()
            with self.assertRaises(sqlite3.OperationalError) as raised:
                self.state.mark_lane_cleaned("fictional-lane")
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            self.assertFalse(self.state._connection.in_transaction)
            self.assertEqual(self.state.get_lane("fictional-lane"), before)
            reader.rollback()
        self.state.mark_lane_cleaned("fictional-lane")
        self.assertIsNotNone(self.state.get_lane("fictional-lane")["cleaned_at"])
        with self.assertRaisesRegex(StateError, "already cleaned"):
            self.state.mark_lane_cleaned("fictional-lane")

    def test_domain_mutations_require_and_retain_caller_transaction(self) -> None:
        from hermes_codex_router.state_lanes import LaneState

        domain = LaneState(self.state._connection)

        def register_in_domain() -> None:
            domain.register_in_transaction(
                lane_id="fictional-lane",
                project_id="example-project",
                worktree_path=str(self.lane),
                branch_name="lane/fictional-lane",
                topic_id=self.topic.topic_id,
                now="fictional-timestamp",
            )

        with self.assertRaisesRegex(StateError, "transaction"):
            register_in_domain()
        before = tuple(self.state._connection.iterdump())
        with self.assertRaisesRegex(RuntimeError, "fictional outer fault"):
            with self.state._immediate_transaction():
                register_in_domain()
                self.assertTrue(self.state._connection.in_transaction)
                with closing(sqlite3.connect(self.path, timeout=0.01)) as observer:
                    self.assertEqual(
                        observer.execute("SELECT count(*) FROM worktree_lanes").fetchone()[0], 0
                    )
                raise RuntimeError("fictional outer fault")
        self.assertEqual(tuple(self.state._connection.iterdump()), before)
        self.assertFalse(self.state._connection.in_transaction)

    def test_lane_domain_cannot_import_or_own_runtime_and_transaction_lifecycle(self) -> None:
        import hermes_codex_router.state_lanes as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {
            "state",
            "service",
            "cli",
            "external_worker",
            "worker",
            "registry",
            "worktrees",
            "subprocess",
            "os",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[-1] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[-1]]
            else:
                names = []
            self.assertFalse(forbidden.intersection(names), names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(
                    node.func.attr, {"connect", "commit", "rollback", "close", "resolve"}
                )


if __name__ == "__main__":
    unittest.main()
