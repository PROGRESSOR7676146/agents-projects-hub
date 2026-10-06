from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from hermes_codex_router.codex_activity import CodexActivityEvent, CodexActivityKind
from hermes_codex_router.schema_task_activity import TASK_ACTIVITY_SCHEMA
from hermes_codex_router.schema_task_lifecycle import TASK_LIFECYCLE_SCHEMA
from hermes_codex_router.task_activity import TaskActivityState
from hermes_codex_router.task_lifecycle import TaskLifecycleState


class TaskActivityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.db.executescript("""
            CREATE TABLE topics(topic_id INTEGER PRIMARY KEY,chat_id INTEGER,thread_id INTEGER,
                project_id TEXT,execution_scope TEXT);
            CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY,topic_id INTEGER,agent_id TEXT,
                generation INTEGER,provider_session_id TEXT,writer_mode TEXT,status TEXT);
            CREATE TABLE provider_jobs(job_id TEXT PRIMARY KEY,topic_id INTEGER,chat_id INTEGER,
                message_id INTEGER,agent_id TEXT,session_id TEXT,session_generation INTEGER,
                status TEXT,lease_token TEXT,lease_expires_at TEXT);
            CREATE TABLE provider_execution_checkpoints(job_id TEXT PRIMARY KEY,
                provider_thread_id TEXT,provider_turn_id TEXT,project_root TEXT,completed_text TEXT);
            CREATE TABLE provider_stop_requests(request_id TEXT PRIMARY KEY,topic_id INTEGER,chat_id INTEGER);
            INSERT INTO topics VALUES(1,-1001234567890,7,'example-project','root:/home/example/project');
            INSERT INTO agent_sessions VALUES('session',1,'codex',1,'thread','telegram','active');
            INSERT INTO provider_jobs VALUES('job',1,-1001234567890,10,'codex','session',1,
                'executing','lease','2026-01-02T00:00:00+00:00');
            INSERT INTO provider_execution_checkpoints VALUES('job','thread','turn','/home/example/project',NULL);
        """)
        self.db.executescript(TASK_LIFECYCLE_SCHEMA + TASK_ACTIVITY_SCHEMA)
        self.db.executescript("""
            ALTER TABLE provider_jobs ADD COLUMN created_at TEXT DEFAULT '2026-01-01T00:00:00+00:00';
            ALTER TABLE provider_stop_requests ADD COLUMN created_at TEXT;
            ALTER TABLE provider_stop_requests ADD COLUMN status TEXT;
            CREATE TABLE provider_job_holds(job_id TEXT,held_at TEXT,decision TEXT,decided_at TEXT);
        """)
        self.notices = TaskLifecycleState(
            self.db, transaction=self.transaction, state_error=ValueError
        )
        self.state = TaskActivityState(
            self.db,
            transaction=self.transaction,
            state_error=ValueError,
            notices=self.notices,
            notices_enabled=True,
        )

    def tearDown(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def bind(self, **kwargs):
        args: dict[str, Any] = dict(
            job_id="job",
            token="lease",
            thread_id="thread",
            turn_id="turn",
            project_root="/home/example/project",
            now=self.now,
        )
        args.update(kwargs)
        return self.state.bind_accepted(**args)

    def test_stale_retirement_cannot_remove_current_binding_entries_or_notice(self) -> None:
        self.bind()
        self.event("approval_requested", "command", request=7)
        before = [
            [tuple(row) for row in self.db.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("task_activity", "task_activity_entries", "task_lifecycle_notices")
        ]
        self.assertFalse(self.state.retire_observation("job", "stale-lease", now=self.now))
        after = [
            [tuple(row) for row in self.db.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("task_activity", "task_activity_entries", "task_lifecycle_notices")
        ]
        self.assertEqual(after, before)

    def event(
        self,
        kind: CodexActivityKind = "visible_message_completed",
        item: str | None = "item",
        request=None,
        category=None,
        seconds=1,
    ):
        category = category or (
            "visible_message" if kind == "visible_message_completed" else "command"
        )
        return self.state.record_activity(
            "job",
            "lease",
            CodexActivityEvent(kind, category, "thread", "turn", item, request),
            now=self.now + timedelta(seconds=seconds),
        )

    def evaluate(self, seconds, **kwargs):
        return self.state.evaluate(now=self.now + timedelta(seconds=seconds), **kwargs)

    def row(self):
        return self.db.execute("SELECT * FROM task_activity").fetchone()

    def test_seed_once_and_notice_once_then_meaningful_progress_rearms(self):
        self.bind()
        self.bind(now=self.now + timedelta(seconds=200))
        self.assertEqual(self.evaluate(299), ())
        self.assertEqual(len(self.evaluate(300)), 1)
        self.assertEqual(self.evaluate(900), ())
        self.event(seconds=901)
        self.assertEqual(self.evaluate(1200), ())
        self.assertEqual(len(self.evaluate(1201)), 1)

    def test_tool_mode_waits_longer_and_completion_restores_ordinary(self):
        self.bind()
        self.event("tool_started")
        self.assertEqual(self.evaluate(301), ())
        self.assertEqual(len(self.evaluate(1201)), 1)
        self.event("tool_completed", seconds=1202)
        self.assertEqual(len(self.evaluate(1502)), 1)

    def test_approval_exact_resolution_restores_tool_without_duplicate_notice(self):
        self.bind()
        self.event("tool_started")
        self.event("approval_requested", request=12, seconds=2)
        self.event("approval_requested", request=12, seconds=3)
        self.assertEqual(self.row()["mode"], "approval")
        self.assertFalse(self.event("approval_resolved", request="12", seconds=4))
        self.assertEqual(self.row()["mode"], "approval")
        self.assertEqual(self.evaluate(4000), ())
        self.assertTrue(self.event("approval_resolved", request=12, seconds=4001))
        self.assertEqual(self.row()["mode"], "tool")
        rows = self.db.execute("SELECT * FROM task_lifecycle_notices").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("Codex/tlive", rows[0]["telegram_html"])
        self.assertNotIn("12", rows[0]["telegram_html"])

    def test_duplicates_and_retries_never_rearm(self):
        self.bind()
        self.event()
        self.event(seconds=200)
        self.event("retrying", item=None, category="retry", seconds=250)
        self.assertEqual(len(self.evaluate(301)), 1)

    def test_parallel_tools_and_approvals_keep_mode_until_all_resolved(self):
        self.bind()
        self.event("tool_started", item="one")
        self.event("tool_started", item="two", seconds=2)
        self.event("tool_completed", item="one", seconds=3)
        self.assertEqual(self.row()["mode"], "tool")
        self.event("approval_requested", item="two", request=1, seconds=4)
        self.event("approval_requested", item="two", request=2, seconds=5)
        self.event("approval_resolved", item="two", request=1, seconds=6)
        self.assertEqual(self.row()["mode"], "approval")
        self.event("approval_resolved", item="two", request=2, seconds=7)
        self.assertEqual(self.row()["mode"], "tool")

    def test_binding_revalidates_session_topic_provider_generation_writer_and_lease(self):
        mutations = (
            "UPDATE agent_sessions SET topic_id=2",
            "UPDATE agent_sessions SET agent_id='other'",
            "UPDATE agent_sessions SET generation=2",
            "UPDATE agent_sessions SET writer_mode='local'",
            "UPDATE agent_sessions SET provider_session_id='other'",
            "UPDATE provider_jobs SET lease_token='other'",
            "UPDATE provider_jobs SET status='indeterminate'",
            "UPDATE provider_execution_checkpoints SET project_root='/home/example/other'",
            "UPDATE topics SET thread_id=8",
        )
        self.bind()
        for sql in mutations:
            with self.subTest(sql=sql):
                self.db.execute("SAVEPOINT changed")
                self.db.execute(sql)
                self.assertEqual(
                    self.state.evaluate_in_transaction(now=self.now + timedelta(seconds=300)), ()
                )
                with self.assertRaises(ValueError):
                    self.state.record_activity_in_transaction(
                        "job",
                        "lease",
                        CodexActivityEvent(
                            "visible_message_completed", "visible_message", "thread", "turn", "item"
                        ),
                        now=self.now,
                    )
                self.db.execute("ROLLBACK TO changed")
                self.db.execute("RELEASE changed")

    def test_bad_initial_binding_and_expired_lease_fail(self):
        for kwargs in (
            {"thread_id": "other"},
            {"turn_id": "other"},
            {"token": "other"},
            {"project_root": "/home/example/other"},
            {"now": self.now + timedelta(days=1)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.bind(**kwargs)
        self.assertIsNone(self.row())

    def test_notice_failure_rolls_back_activity(self):
        self.bind()
        original = self.notices.prepare_notice_in_transaction

        def fail(**kwargs):
            raise RuntimeError("fault")

        self.notices.prepare_notice_in_transaction = fail
        with self.assertRaises(RuntimeError):
            self.event("approval_requested", request=1)
        self.notices.prepare_notice_in_transaction = original
        self.assertEqual(self.row()["mode"], "ordinary")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM task_activity_entries").fetchone()[0], 0
        )

    def test_restart_retains_episode_and_approval_dedup(self):
        self.bind()
        self.evaluate(300)
        self.state = TaskActivityState(
            self.db,
            transaction=self.transaction,
            state_error=ValueError,
            notices=self.notices,
            notices_enabled=True,
        )
        self.assertEqual(self.evaluate(600), ())
        self.event("approval_requested", request=1, seconds=601)
        self.event("approval_requested", request=1, seconds=602)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM task_lifecycle_notices").fetchone()[0], 2
        )

    def test_total_metadata_bound_does_not_reset_progress_or_break_existing_resolution(self):
        self.bind()
        self.event("approval_requested", request=1)
        for i in range(511):
            self.event(item=f"message-{i}", seconds=i + 2)
        self.assertFalse(self.event(item="overflow", seconds=600))
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM task_activity_entries").fetchone()[0], 512
        )
        self.assertTrue(self.event("approval_resolved", request=1, seconds=601))
        self.assertEqual(self.row()["mode"], "ordinary")

    def test_disabled_notice_delivery_still_records_activity(self):
        self.state.notices_enabled = False
        self.bind()
        self.event("approval_requested", request=1)
        self.assertEqual(self.row()["mode"], "approval")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM task_lifecycle_notices").fetchone()[0], 0
        )

    def test_callers_transaction_rolls_back_seed(self):
        with self.assertRaises(RuntimeError):
            with self.transaction():
                self.state.bind_accepted_in_transaction(
                    "job", "lease", "thread", "turn", "/home/example/project", now=self.now
                )
                raise RuntimeError("fault")
        self.assertIsNone(self.row())

    def test_progress_supersedes_only_unattempted_warning_and_resolution_supersedes_approval(self):
        self.bind()
        warning = self.evaluate(300)[0]
        self.event(seconds=301)
        self.assertEqual(self.notices.get_notice(warning.notice_id).status, "superseded")
        second = self.evaluate(601)[0]
        lease = self.notices.lease_notice("sender", now=self.now + timedelta(seconds=601))
        assert lease is not None and lease.lease_token is not None
        self.notices.begin_send(
            lease.notice_id, lease.lease_token, now=self.now + timedelta(seconds=601)
        )
        self.event(item="next", seconds=602)
        self.assertEqual(self.notices.get_notice(second.notice_id).status, "leased")
        self.event("approval_requested", request=1, seconds=603)
        self.event("approval_resolved", request=1, seconds=604)
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM task_lifecycle_notices WHERE kind='approval_wait'"
            ).fetchone()[0],
            "superseded",
        )

    def test_tool_output_updates_progress_without_persisting_payload_and_stops_after_completion(
        self,
    ):
        self.bind()
        self.assertFalse(self.event("tool_output"))
        self.event("tool_started")
        self.event("tool_output", seconds=1000)
        self.assertEqual(self.evaluate(1201), ())
        self.event("tool_completed", seconds=1001)
        self.assertFalse(self.event("tool_output", seconds=1100))
        self.assertEqual(len(self.evaluate(1301)), 1)

    def test_invalid_clocks_thresholds_and_other_turn_cannot_mutate_state(self):
        self.bind()
        with self.assertRaises(ValueError):
            self.bind(now=datetime(2026, 1, 1))
        for threshold in (0, True, -1):
            with self.assertRaises(ValueError):
                self.evaluate(300, ordinary_seconds=threshold)
        with self.assertRaises(ValueError):
            self.state.record_activity(
                "job",
                "lease",
                CodexActivityEvent("tool_started", "command", "thread", "other", "item"),
                now=self.now,
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM task_activity_entries").fetchone()[0], 0
        )

    def test_changed_topic_scope_rejects_bind_update_and_evaluation(self):
        with self.transaction():
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        with self.assertRaises(ValueError):
            self.bind()
        with self.transaction():
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/project'")
        self.bind()
        with self.transaction():
            self.db.execute("UPDATE topics SET execution_scope='project:example-project'")
        with self.assertRaises(ValueError):
            self.event()
        self.assertEqual(self.evaluate(300), ())

    def test_legacy_project_scope_is_snapshotted_and_project_change_invalidates(self):
        with self.transaction():
            self.db.execute("UPDATE topics SET execution_scope=NULL")
        self.bind()
        self.assertEqual(self.row()["execution_scope"], "project:example-project")
        with self.transaction():
            self.db.execute("UPDATE topics SET project_id='other-project'")
        self.assertEqual(self.evaluate(300), ())
        with self.assertRaises(ValueError):
            self.event()
