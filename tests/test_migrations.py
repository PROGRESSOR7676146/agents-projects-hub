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
    def test_migration_handles_legacy_writer_column_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            connection = sqlite3.connect(path)
            connection.executescript(MIGRATION_1)
            connection.execute("ALTER TABLE agent_sessions DROP COLUMN writer_mode")
            connection.execute(
                "ALTER TABLE agent_sessions ADD COLUMN writer_mode TEXT NOT NULL DEFAULT 'telegram'"
            )
            connection.execute("PRAGMA user_version = 6")
            connection.execute(
                """INSERT INTO topics
                   (project_id, chat_id, thread_id, title, created_at, updated_at)
                   VALUES ('p', -1001234567890, 7, 'Topic', 'now', 'now')"""
            )
            connection.execute(
                """INSERT INTO agent_sessions
                   (session_id, topic_id, agent_id, generation, status, model, effort,
                    created_at, updated_at, writer_mode)
                   VALUES ('s', 1, 'codex', 1, 'active', 'm', 'high',
                           'created', 'updated', 'telegram')"""
            )
            connection.commit()
            connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual(result.current_version, LATEST_SCHEMA_VERSION)
            migrated = sqlite3.connect(path)
            try:
                row = migrated.execute(
                    "SELECT session_id, writer_mode, created_at, updated_at FROM agent_sessions"
                ).fetchone()
                self.assertEqual(row, ("s", "telegram", "created", "updated"))
            finally:
                migrated.close()

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
                sql = migrated.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_sessions'"
                ).fetchone()[0]
                self.assertIn("'local'", sql)
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

    def test_queue_migrations_are_additive_and_preserve_legacy_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO topics
                       (project_id, chat_id, thread_id, title, created_at, updated_at)
                       VALUES ('example-project', -1001234567890, 7, 'Topic', 'now', 'now')"""
                )
                connection.execute(
                    """INSERT INTO turn_dispatches
                       (dispatch_id, chat_id, message_id, topic_id, agent_id, status,
                        created_at, updated_at)
                       VALUES ('legacy-dispatch', -1001234567890, 42, 1, 'codex',
                               'completed', 'now', 'now')"""
                )
                connection.executescript(
                    """DROP TABLE telegram_outbox;
                       DROP TABLE provider_job_results;
                       DROP TABLE provider_jobs;
                       DROP TABLE topic_queue_counters;
                       PRAGMA user_version = 9;"""
                )
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual(
                (result.previous_version, result.current_version), (9, LATEST_SCHEMA_VERSION)
            )
            migrated = sqlite3.connect(path)
            try:
                self.assertEqual(
                    migrated.execute(
                        "SELECT status FROM turn_dispatches WHERE dispatch_id = 'legacy-dispatch'"
                    ).fetchone()[0],
                    "completed",
                )
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(
                    {
                        "provider_jobs",
                        "provider_job_results",
                        "telegram_outbox",
                        "topic_queue_counters",
                        "turn_dispatches",
                        "runtime_health",
                        "provider_job_inputs",
                        "provider_stop_requests",
                        "provider_job_absorptions",
                    }.issubset(tables)
                )
                self.assertIsNotNone(
                    migrated.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'trigger'
                             AND name = 'provider_jobs_context_watermark_topic'"""
                    ).fetchone()
                )
            finally:
                migrated.close()

    def test_version_14_repairs_early_version_13_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    """DROP TABLE provider_job_absorptions;
                       DROP TABLE provider_stop_requests;
                       PRAGMA user_version = 13;"""
                )
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual((result.previous_version, result.current_version), (13, 14))
            migrated = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("provider_stop_requests", tables)
                self.assertIn("provider_job_absorptions", tables)
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
