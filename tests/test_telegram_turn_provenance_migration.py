"""Schema49 upgrades retain all prior evidence without importing authority."""

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


class TelegramTurnProvenanceMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexTurnControlJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.journal.record_turn(self.fixture.job_id, self.fixture.token, "example-turn")
        state = self.fixture.state
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        owner = state.telegram_ingress.register(
            "hub", instance_token="example-original-publisher", previous_epoch=0, now=now
        )
        state.telegram_ingress.record_poll(owner, sequence=1, succeeded=True, observed_at=now)
        with state._immediate_transaction():
            state._connection.execute(
                """UPDATE codex_turn_controls SET send_owner_token_hash=?,send_started_at=?,
                   interrupt_source='protective',interrupt_outcome='unknown'""",
                ("a" * 64, now.isoformat()),
            )
        self.path = self.fixture.fixture.config.state_path.with_name("example-historical49.db")
        project_historical_database(self.fixture.fixture.config.state_path, self.path, 49)
        with closing(sqlite3.connect(self.path)) as old:
            self.old_columns = columns(old)
            self.before = rows(old, self.old_columns)
            self.old_objects = objects(old)

    def test_upgrade_preserves_history_polling_and_unknown_sender_with_empty_sidecars(self):
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (49, 50))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(rows(backup, self.old_columns), self.before)
            self.assertEqual(objects(backup), self.old_objects)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 49)
        with closing(sqlite3.connect(self.path)) as upgraded:
            self.assertEqual(rows(upgraded, self.old_columns), self.before)
            for name in ("provider_job_telegram_ingress", "codex_telegram_precaution_targets"):
                self.assertEqual(upgraded.execute(f"SELECT * FROM {name}").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_ddl_failure_rolls_back_all_schema_objects_and_existing_evidence(self):
        with (
            patch.object(
                migrations, "MIGRATION_50", migrations.MIGRATION_50 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(rows(old, self.old_columns), self.before)
            self.assertEqual(objects(old), self.old_objects)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 49)


if __name__ == "__main__":
    unittest.main()
