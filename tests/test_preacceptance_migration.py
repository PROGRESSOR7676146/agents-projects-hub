from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState


class PreacceptanceMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state.db"
        self.now = datetime.now(timezone.utc)
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 39):
            state = HubState.open(self.path, codex_permission_profile=None)
        try:
            topic = state.observe_topic(
                project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
            )
            session = state.activate_agent(topic.topic_id, "codex", "example-model", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="example-input",
                chat_id=topic.chat_id,
                message_id=1,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="Example task",
            )
            for index, status in enumerate(("delivered", "unknown", "attempted")):
                with state._immediate_transaction():
                    notice, _ = state.task_notices.prepare_notice_in_transaction(
                        event_key=f"example-notice-{index}",
                        kind="accepted",
                        job_id=job.job_id,
                        chat_id=topic.chat_id,
                        thread_id=topic.thread_id,
                        telegram_html="Example notice",
                        now=self.now,
                    )
                leased = state.task_notices.lease_notice("example-sender", now=self.now)
                assert leased is not None and leased.lease_token is not None
                self.assertEqual(leased.notice_id, notice.notice_id)
                state.task_notices.begin_send(leased.notice_id, leased.lease_token, now=self.now)
                if status == "delivered":
                    state.task_notices.complete_send(
                        leased.notice_id, leased.lease_token, telegram_message_id=101, now=self.now
                    )
                elif status == "unknown":
                    state.task_notices.mark_send_unknown(
                        leased.notice_id,
                        leased.lease_token,
                        error_code="example_timeout",
                        now=self.now,
                    )
            state.enqueue_provider_job(
                idempotency_key="example-queued-tail",
                chat_id=topic.chat_id,
                message_id=2,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="Example queued tail",
            )
            executing = state.lease_provider_job("codex", "example-worker", now=self.now)
            assert executing is not None and executing.lease_token is not None
            self.assertEqual(executing.job_id, job.job_id)
            state.mark_provider_job_executing(job.job_id, executing.lease_token)
            journal = ExecutionJournal(state)
            journal.record_thread(
                job.job_id,
                executing.lease_token,
                "example-thread",
                self.path.parent,
                codex_permission_profile=None,
            )
            journal.record_turn(job.job_id, executing.lease_token, "example-turn")
        finally:
            state.close()

    def snapshot(self, connection):
        return {
            row[0]: connection.execute(f'SELECT * FROM "{row[0]}"').fetchall()
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def test_additive_upgrade_preserves_attempted_unknown_and_delivered_evidence(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = self.snapshot(old)
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 40):
            result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (39, 40))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(self.snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 39)
        with sqlite3.connect(self.path) as upgraded:
            after = self.snapshot(upgraded)
            for table, rows in before.items():
                self.assertEqual(after[table], rows, table)
            self.assertEqual(
                set(after) - set(before),
                {"preacceptance_runtime_epochs", "preacceptance_scopes", "preacceptance_requests"},
            )
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_ddl_failure_rolls_back_all_new_tables_and_version(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = self.snapshot(old)
        with patch.object(
            migrations, "MIGRATION_40", migrations.MIGRATION_40 + "\nINVALID EXAMPLE SQL;"
        ):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate_database(self.path, create_backup=False)
        with sqlite3.connect(self.path) as unchanged:
            self.assertEqual(self.snapshot(unchanged), before)
            self.assertEqual(unchanged.execute("PRAGMA user_version").fetchone()[0], 39)
