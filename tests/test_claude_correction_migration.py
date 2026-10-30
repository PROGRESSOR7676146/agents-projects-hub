from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from hermes_codex_router import migrations
from tests import test_preacceptance_migration as fixtures


class ClaudeCorrectionMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = fixtures.PreacceptanceMigrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.path = fixture.path
        self.snapshot = fixture.snapshot
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 54):
            migrations.migrate_database(self.path, create_backup=False)

    def test_additive_upgrade_and_consistent_private_backup(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = self.snapshot(old)
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (54, 55))
        assert result.backup_path
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(self.snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 54)
        with sqlite3.connect(self.path) as current:
            after = self.snapshot(current)
            self.assertEqual(
                set(after) - set(before), {"claude_input_runs", "claude_input_children"}
            )
            for table, rows in before.items():
                self.assertEqual(after[table], rows, table)
            self.assertEqual(current.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_failure_keeps_old_rows_and_schema(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = self.snapshot(old)
        with patch.object(
            migrations, "MIGRATION_55", migrations.MIGRATION_55 + "\nINVALID EXAMPLE SQL;"
        ):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate_database(self.path, create_backup=False)
        with sqlite3.connect(self.path) as current:
            self.assertEqual(self.snapshot(current), before)
            self.assertEqual(current.execute("PRAGMA user_version").fetchone()[0], 54)


if __name__ == "__main__":
    unittest.main()
