from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATION_1,
    backup_database,
    migrate_database,
)


class MigrationTests(unittest.TestCase):
    def test_migration_preserves_legacy_data_and_creates_private_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            connection = sqlite3.connect(path)
            connection.executescript(MIGRATION_1)
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                """INSERT INTO topics
                   (project_id, chat_id, thread_id, title, created_at, updated_at)
                   VALUES ('p', -1001234567890, 7, 'Topic', 'now', 'now')"""
            )
            connection.commit()
            connection.close()

            result = migrate_database(path)

            self.assertEqual(result.previous_version, 1)
            self.assertEqual(result.current_version, LATEST_SCHEMA_VERSION)
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
            migrated = sqlite3.connect(path)
            try:
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 1)
                self.assertEqual(
                    migrated.execute("PRAGMA user_version").fetchone()[0],
                    LATEST_SCHEMA_VERSION,
                )
            finally:
                migrated.close()

    def test_explicit_backup_is_sqlite_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            destination = backup_database(path, Path(directory) / "copy.db")
            connection = sqlite3.connect(destination)
            try:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
