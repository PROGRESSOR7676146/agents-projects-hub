from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from hermes_codex_router import migrations
from tests import test_preacceptance_migration as fixtures


class PreexecutionRetryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = fixtures.PreacceptanceMigrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.path = fixture.path
        self.enterContext(patch.object(migrations, "LATEST_SCHEMA_VERSION", 41))
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 40):
            migrations.migrate_database(self.path, create_backup=False)

    @staticmethod
    def snapshot(connection):
        return {
            row[0]: connection.execute(f'SELECT * FROM "{row[0]}"').fetchall()
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def test_additive_upgrade_backup_preserves_all_existing_work_and_receipts(self) -> None:
        with sqlite3.connect(self.path) as connection:
            before = self.snapshot(connection)
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (40, 41))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(self.snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 40)
        with sqlite3.connect(self.path) as connection:
            after = self.snapshot(connection)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='provider_preexecution_retry_ticket_immutable'"
                ).fetchone()
            )
            self.assertEqual(
                set(after) - set(before),
                {
                    "provider_preexecution_retry_tickets",
                    "provider_preexecution_retries",
                    "provider_preexecution_retry_controls",
                },
            )
            for table, rows in before.items():
                self.assertEqual(after[table], rows, table)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        repeated = migrations.migrate_database(self.path)
        self.assertEqual((repeated.previous_version, repeated.current_version), (41, 41))
        self.assertIsNone(repeated.backup_path)
        with sqlite3.connect(self.path) as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='provider_preexecution_retry_ticket_immutable'"
                ).fetchone()
            )

    def test_fault_rolls_back_tables_and_version_without_replacing_live_database(self) -> None:
        with sqlite3.connect(self.path) as connection:
            before = self.snapshot(connection)
        with patch.object(
            migrations, "MIGRATION_41", migrations.MIGRATION_41 + "\nINVALID EXAMPLE SQL;"
        ):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate_database(self.path, create_backup=False)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(self.snapshot(connection), before)
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='provider_preexecution_retry_ticket_immutable'"
                ).fetchone()
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 40)
