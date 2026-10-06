from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.codex_activity import CodexActivityEvent
from hermes_codex_router.hub_config import HubConfigError, HubTelegramBot, load_hub_config
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.state import HubState
from hermes_codex_router.task_lifecycle import TaskLifecycleState
from hermes_codex_router.worker_activity import codex_activity_for_turn
from tests import test_hub_config, test_outbox_sender


class TaskActivityConfigIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_hub_config.HubConfigTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_defaults_and_explicit_thresholds(self):
        default = load_hub_config(self.fixture.write_config())
        self.assertEqual(default.task_no_progress_seconds, 300)
        self.assertEqual(default.task_tool_no_progress_seconds, 1200)
        configured = load_hub_config(
            self.fixture.write_config(
                task_no_progress_seconds=1, task_tool_no_progress_seconds=86400
            )
        )
        self.assertEqual(configured.task_no_progress_seconds, 1)
        self.assertEqual(configured.task_tool_no_progress_seconds, 86400)

    def test_thresholds_reject_boolean_noninteger_and_out_of_range(self):
        for key in ("task_no_progress_seconds", "task_tool_no_progress_seconds"):
            for value in (True, False, 0, -1, "300", 1.5, 86401):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(HubConfigError, key),
                ):
                    load_hub_config(self.fixture.write_config(**{key: value}))


class TaskActivityDatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_outbox_sender.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.path = self.fixture.config.state_path

    def tearDown(self):
        self.fixture.tearDown()

    def enqueue(self, state, message_id):
        topic = state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=7, title="Example"
        )
        session = state.activate_agent(topic.topic_id, "opencode", "model", "high")
        job, _ = state.enqueue_provider_job(
            idempotency_key=f"activity:{message_id}",
            chat_id=topic.chat_id,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Fictional task",
        )
        return job

    def seed_v36(self):
        from tests.schema_fixtures import legacy_selection_columns

        with (
            patch.object(migrations, "LATEST_SCHEMA_VERSION", 36),
            legacy_selection_columns(self.path),
        ):
            self.fixture.ready_outbox("antigravity", 501)
            state = HubState.open(self.path, codex_permission_profile=None)
            try:
                job = self.enqueue(state, 502)
                leased = state.lease_provider_job("opencode", "example-worker")
                assert leased is not None and leased.lease_token is not None
                state.mark_provider_job_executing(job.job_id, leased.lease_token)
                self.enqueue(state, 503)
                notices = TaskLifecycleState(
                    state._connection,
                    transaction=state._immediate_transaction,
                    state_error=ValueError,
                )
                with state._immediate_transaction():
                    notice, _ = notices.prepare_notice_in_transaction(
                        event_key="unknown-example",
                        kind="no_progress",
                        job_id=job.job_id,
                        chat_id=-1001234567890,
                        thread_id=7,
                        telegram_html="Fictional warning",
                        now=datetime.now(timezone.utc),
                    )
                    state._connection.execute(
                        "UPDATE task_lifecycle_notices SET status='unknown',attempt_count=1 WHERE notice_id=?",
                        (notice.notice_id,),
                    )
            finally:
                state.close()

    def snapshot(self, connection):
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "provider_jobs",
                "telegram_outbox",
                "telegram_outbox_parts",
                "task_lifecycle_notices",
            )
        }

    def test_schema_36_to_37_backup_preserves_all_execution_and_delivery_rows(self):
        self.seed_v36()
        with sqlite3.connect(self.path) as connection:
            before = self.snapshot(connection)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 36)
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 37):
            result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (36, 37))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 36)
            self.assertEqual(self.snapshot(connection), before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(self.snapshot(connection), before)
            for table in ("task_activity", "task_activity_entries"):
                self.assertEqual(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
                )

    def test_schema_37_ddl_fault_rolls_back_in_place(self):
        self.seed_v36()
        with sqlite3.connect(self.path) as connection:
            before = self.snapshot(connection)
        original = migrations._execute_migration_script

        def fail_after_activity_schema(connection, script):
            original(connection, script)
            if "CREATE TABLE IF NOT EXISTS task_activity (" in script:
                raise RuntimeError("fictional activity migration fault")

        with patch.object(
            migrations, "_execute_migration_script", side_effect=fail_after_activity_schema
        ):
            with self.assertRaisesRegex(RuntimeError, "activity migration fault"):
                migrations.migrate_database(self.path)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 36)
            self.assertEqual(self.snapshot(connection), before)
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='task_activity'"
                ).fetchone()
            )

    def test_sender_observes_worker_activity_passively_and_honors_custom_threshold(self):
        self.check_sender_activity(tool=False)

    def test_sender_honors_longer_custom_tool_threshold(self):
        self.check_sender_activity(tool=True)

    def check_sender_activity(self, *, tool: bool):
        threshold = 71 if tool else 17
        config = replace(
            self.fixture.config,
            hub_bot=HubTelegramBot("example_hub_bot", self.fixture.base / "unused-token"),
            task_no_progress_seconds=17,
            task_tool_no_progress_seconds=71,
        )
        now = datetime.now(timezone.utc)
        state = HubState.open(self.path, codex_permission_profile=None)
        try:
            job = self.enqueue(state, 601)
            lease = state.lease_provider_job("opencode", "example-worker", lease_seconds=3600)
            assert lease is not None and lease.lease_token is not None
            state.mark_provider_job_executing(job.job_id, lease.lease_token)
            with state._immediate_transaction():
                state._connection.execute(
                    "UPDATE agent_sessions SET provider_session_id='thread' WHERE session_id=?",
                    (job.session_id,),
                )
                state._connection.execute(
                    "INSERT INTO provider_execution_checkpoints(job_id,provider_thread_id,provider_turn_id,project_root,updated_at) VALUES(?,?,?,?,?)",
                    (job.job_id, "thread", "turn", "/home/example/project", now.isoformat()),
                )
            client: Any = SimpleNamespace(on_activity=None)
            with codex_activity_for_turn(
                client,
                state,
                config,
                job.job_id,
                lease.lease_token,
                Path("/home/example/project"),
                clock=lambda: now,
            ) as accepted:
                accepted("thread", "turn")
                if tool:
                    client.on_activity(
                        CodexActivityEvent("tool_started", "command", "thread", "turn", "tool-item")
                    )
            before = tuple(
                state._connection.execute(
                    "SELECT status,lease_token,lease_owner FROM provider_jobs WHERE job_id=?",
                    (job.job_id,),
                ).fetchone()
            )
        finally:
            state.close()
        hub = test_outbox_sender.Bot()
        sender = TelegramOutboxSender(
            config,
            telegram_bots={
                "hub": hub,
                "opencode": test_outbox_sender.Bot(),
                "antigravity": test_outbox_sender.Bot(),
            },
        )
        try:
            with patch(
                "hermes_codex_router.codex_appserver.CodexAppServerClient.start_turn",
                side_effect=AssertionError("passive sender invoked provider"),
            ):
                sender.run_cycle(now=now + timedelta(seconds=threshold - 1))
                self.assertEqual(hub.sent, [])
                sender.run_cycle(now=now + timedelta(seconds=threshold))
                self.assertEqual(len(hub.sent), 1)
                self.assertIn("No new progress", hub.sent[0][2])
                sender.run_cycle(now=now + timedelta(seconds=threshold + 1))
                self.assertEqual(len(hub.sent), 1)
            after = tuple(
                sender.state._connection.execute(
                    "SELECT status,lease_token,lease_owner FROM provider_jobs WHERE job_id=?",
                    (job.job_id,),
                ).fetchone()
            )
            self.assertEqual(after, before)
            self.assertEqual(
                sender.state._connection.execute(
                    "SELECT status FROM task_lifecycle_notices WHERE kind='no_progress'"
                ).fetchone()[0],
                "delivered",
            )
        finally:
            sender.close()
