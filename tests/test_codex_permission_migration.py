from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.schema_codex_permissions import PROFILE_TABLES
from hermes_codex_router.state import HubState
from tests.schema_fixtures import project_historical_database


class CodexPermissionMigrationTests(unittest.TestCase):
    def create_v38(self, path: Path):
        source = path.with_name("example-current-seed.db")
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 48):
            state = HubState.open(source, codex_permission_profile=None)
            try:
                topic = state.observe_topic(
                    project_id="example-project",
                    chat_id=-1001234567890,
                    thread_id=7,
                    title="Example topic",
                )
                session = state.activate_agent(topic.topic_id, "codex", "example-model", "low")
                job, _ = state.enqueue_provider_job(
                    idempotency_key="example:1",
                    chat_id=topic.chat_id,
                    message_id=1,
                    topic_id=topic.topic_id,
                    agent_id="codex",
                    session_id=session.session_id,
                    session_generation=session.generation,
                    model=session.model,
                    effort=session.effort,
                    payload_text="Example queued work",
                )
            finally:
                state.close()
        project_historical_database(source, path, 38)
        return session, job

    def snapshot(self, connection):
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        }

    def test_additive_upgrade_keeps_old_queued_work_legacy_and_private_backup(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state.db"
        session, job = self.create_v38(path)
        with sqlite3.connect(path) as old:
            before = self.snapshot(old)
        result = migrations.migrate_database(path)
        self.assertEqual(
            (result.previous_version, result.current_version),
            (38, migrations.LATEST_SCHEMA_VERSION),
        )
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 38)
            self.assertEqual(self.snapshot(backup), before)
        with sqlite3.connect(path) as upgraded:
            after = self.snapshot(upgraded)
            for table in before:
                expected = (
                    [(*row, None) for row in before[table]]
                    if table in PROFILE_TABLES
                    else before[table]
                )
                self.assertEqual(after[table], expected)
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
        state = HubState.open(path, codex_permission_profile="example-project-policy")
        try:
            self.assertIsNone(state.get_session(session.session_id).codex_permission_profile)
            self.assertIsNone(state.get_provider_job(job.job_id).codex_permission_profile)
        finally:
            state.close()

    def test_fault_rolls_back_added_columns_triggers_and_version(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state.db"
        self.create_v38(path)
        with sqlite3.connect(path) as old:
            before = self.snapshot(old)
        with (
            patch.object(
                migrations, "MIGRATION_39", migrations.MIGRATION_39 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(path, create_backup=False)
        with sqlite3.connect(path) as unchanged:
            self.assertEqual(self.snapshot(unchanged), before)
            self.assertEqual(unchanged.execute("PRAGMA user_version").fetchone()[0], 38)
            for table in PROFILE_TABLES:
                self.assertNotIn(
                    "codex_permission_profile",
                    {row[1] for row in unchanged.execute(f"PRAGMA table_info({table})")},
                )
            self.assertEqual(
                unchanged.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE '%_codex_profile_immutable'"
                ).fetchone()[0],
                0,
            )
