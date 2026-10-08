"""Additive schema49 keeps native targets, sender fences and historical health."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

from hermes_codex_router import migrations
from tests import test_codex_turn_controls as fixtures
from tests.schema_fixtures import project_historical_database
from tests.test_codex_control_migration import columns, rows
from tests.test_delivery_control_activation_migration import objects


class TelegramIngressMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexTurnControlJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.journal.record_turn(self.fixture.job_id, self.fixture.token, "example-turn")
        state = self.fixture.state
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.upsert_runtime_health(
            component="controller",
            instance_id="example-controller",
            pid=1234,
            process_start_marker="example-start",
            started_at=now,
            heartbeat_at=now,
            success_at=now,
        )
        state._connection.execute(
            """UPDATE codex_turn_controls SET send_owner_token_hash=?,
            send_started_at=?,interrupt_source='protective',interrupt_outcome='unknown'""",
            ("a" * 64, now.isoformat()),
        )
        state._connection.commit()
        self.path = self.fixture.fixture.config.state_path.with_name("example-historical48.db")
        project_historical_database(self.fixture.fixture.config.state_path, self.path, 48)
        with closing(sqlite3.connect(self.path)) as old:
            self.old_columns = columns(old)
            self.before = rows(old, self.old_columns)
            self.old_objects = objects(old)

    def test_upgrade_preserves_all_rows_fences_and_backup_without_health_import(self):
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 49):
            result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (48, 49))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(rows(backup, self.old_columns), self.before)
            self.assertEqual(objects(backup), self.old_objects)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 48)
        with closing(sqlite3.connect(self.path)) as current:
            self.assertEqual(rows(current, self.old_columns), self.before)
            self.assertEqual(current.execute("SELECT * FROM telegram_group_ingress").fetchall(), [])
            self.assertEqual(current.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(current.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 49):
            self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_ddl_fault_rolls_back_schema_objects_and_all_historical_state(self):
        with (
            patch.object(
                migrations, "MIGRATION_49", migrations.MIGRATION_49 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(rows(old, self.old_columns), self.before)
            self.assertEqual(objects(old), self.old_objects)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 48)


if __name__ == "__main__":
    unittest.main()
