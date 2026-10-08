"""Schema 42 preserves existing authority, execution and transport evidence."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from hermes_codex_router import migrations
from tests import test_preacceptance_migration as fixtures


class ClaudeActivityMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = fixtures.PreacceptanceMigrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.path = fixture.path
        self.snapshot = fixture.snapshot
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 41):
            migrations.migrate_database(self.path, create_backup=False)

    def test_additive_upgrade_preserves_rows_and_private_consistent_backup(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = self.snapshot(old)
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (41, 42))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(self.snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 41)
        with sqlite3.connect(self.path) as upgraded:
            after = self.snapshot(upgraded)
            self.assertEqual(set(after) - set(before), {"claude_activity_observations"})
            for table, rows in before.items():
                self.assertEqual(after[table], rows, table)
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
        repeated = migrations.migrate_database(self.path)
        self.assertEqual((repeated.previous_version, repeated.current_version), (42, 42))
        self.assertIsNone(repeated.backup_path)

    def test_ddl_fault_rolls_back_new_table_and_version(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = self.snapshot(old)
        with patch.object(
            migrations, "MIGRATION_42", migrations.MIGRATION_42 + "\nINVALID EXAMPLE SQL;"
        ):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate_database(self.path, create_backup=False)
        with sqlite3.connect(self.path) as retained:
            self.assertEqual(self.snapshot(retained), before)
            self.assertEqual(retained.execute("PRAGMA user_version").fetchone()[0], 41)
