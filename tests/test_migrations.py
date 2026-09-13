from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from schema_fixtures import remove_adoption_schema

from hermes_codex_router import migrations as migrations_module
from hermes_codex_router.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATION_1,
    MIGRATION_12,
    MIGRATION_19,
    backup_database,
    migrate_database,
)


class MigrationTests(unittest.TestCase):
    @staticmethod
    def _assert_closed(connection: sqlite3.Connection) -> None:
        try:
            with unittest.TestCase().assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def test_backup_closes_source_when_destination_open_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            destination = Path(directory) / "destination.db"
            initial = sqlite3.connect(source)
            initial.close()
            original_connect = sqlite3.connect
            created: list[sqlite3.Connection] = []

            def fail_destination_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                if created:
                    # Another writer won the destination path after the existence check.
                    competitor = original_connect(destination)
                    try:
                        competitor.execute("CREATE TABLE marker (value TEXT)")
                        competitor.execute("INSERT INTO marker VALUES ('fictional competitor')")
                        competitor.commit()
                    finally:
                        competitor.close()
                    raise RuntimeError("destination connection fault")
                connection = original_connect(*args, **kwargs)
                created.append(connection)
                return connection

            with mock.patch.object(
                migrations_module.sqlite3, "connect", side_effect=fail_destination_connect
            ):
                with self.assertRaisesRegex(RuntimeError, "destination connection fault"):
                    backup_database(source, destination)

            self.assertEqual(len(created), 1)
            self._assert_closed(created[0])
            self.assertTrue(destination.is_file())
            competitor = original_connect(destination)
            try:
                self.assertEqual(
                    competitor.execute("SELECT value FROM marker").fetchone()[0],
                    "fictional competitor",
                )
            finally:
                competitor.close()

    def test_backup_closes_connections_before_removing_failed_destination(self) -> None:
        class BrokenBackupConnection(sqlite3.Connection):
            def backup(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("backup copy fault")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            destination = Path(directory) / "destination.db"
            original_connect = sqlite3.connect
            original_unlink = Path.unlink
            initial = original_connect(source)
            initial.close()
            created: list[sqlite3.Connection] = []

            def capture_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                if not created:
                    kwargs["factory"] = BrokenBackupConnection
                connection = original_connect(*args, **kwargs)
                created.append(connection)
                return connection

            def require_closed_before_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
                if path == destination:
                    for connection in created:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            connection.execute("SELECT 1")
                original_unlink(path, *args, **kwargs)

            try:
                with (
                    mock.patch.object(
                        migrations_module.sqlite3, "connect", side_effect=capture_connect
                    ),
                    mock.patch.object(Path, "unlink", require_closed_before_unlink),
                ):
                    with self.assertRaisesRegex(RuntimeError, "backup copy fault"):
                        backup_database(source, destination)
                self.assertEqual(len(created), 2)
                self.assertFalse(destination.exists())
            finally:
                for connection in created:
                    connection.close()

    def test_migration_preserves_backup_error_after_closing_prebackup_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA user_version = 1")
            finally:
                connection.close()

            with mock.patch.object(
                migrations_module, "backup_database", side_effect=RuntimeError("backup fault")
            ):
                with self.assertRaisesRegex(RuntimeError, "backup fault"):
                    migrate_database(path)

    def test_progress_delivery_migration_is_additive_from_v23(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                remove_adoption_schema(connection)
                connection.execute("DROP TABLE IF EXISTS provider_progress_deliveries")
                connection.execute("PRAGMA user_version = 23")
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual(
                (result.previous_version, result.current_version), (23, LATEST_SCHEMA_VERSION)
            )
            migrated = sqlite3.connect(path)
            try:
                columns = {
                    row[1]
                    for row in migrated.execute("PRAGMA table_info(provider_progress_deliveries)")
                }
                self.assertEqual(
                    columns,
                    {
                        "progress_id",
                        "item_sequence",
                        "job_id",
                        "sender_agent_id",
                        "chat_id",
                        "thread_id",
                        "telegram_html",
                        "status",
                        "attempt_count",
                        "available_at",
                        "lease_owner",
                        "lease_token",
                        "lease_expires_at",
                        "telegram_message_id",
                        "error_code",
                        "created_at",
                        "updated_at",
                        "delivered_at",
                    },
                )
                self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                migrated.close()

    def test_execution_scope_migration_backfills_v25_topics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP INDEX topics_execution_scope")
                connection.execute("ALTER TABLE topics DROP COLUMN execution_scope")
                connection.execute(
                    """INSERT INTO topics
                       (project_id, chat_id, thread_id, title, created_at, updated_at)
                       VALUES ('example-project', -1001234567890, 7, 'Topic', 'now', 'now')"""
                )
                connection.execute("PRAGMA user_version = 25")
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual((result.previous_version, result.current_version), (25, 26))
            migrated = sqlite3.connect(path)
            try:
                self.assertEqual(
                    migrated.execute("SELECT execution_scope FROM topics").fetchone()[0],
                    "project:example-project",
                )
                self.assertIsNotNone(
                    migrated.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='index' "
                        "AND name='topics_execution_scope'"
                    ).fetchone()
                )
            finally:
                migrated.close()

    def test_execution_scope_migration_fault_rolls_back_added_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP INDEX topics_execution_scope")
                connection.execute("ALTER TABLE topics DROP COLUMN execution_scope")
                connection.execute("PRAGMA user_version = 25")
                connection.commit()
            finally:
                connection.close()

            with mock.patch.object(
                migrations_module,
                "_execute_migration_script",
                side_effect=RuntimeError("fictional migration fault"),
            ):
                with self.assertRaisesRegex(RuntimeError, "fictional migration fault"):
                    migrate_database(path, create_backup=False)

            restored = sqlite3.connect(path)
            try:
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 25)
                columns = {row[1] for row in restored.execute("PRAGMA table_info(topics)")}
                self.assertNotIn("execution_scope", columns)
            finally:
                restored.close()

    def test_indeterminate_resolution_migration_is_additive_from_v22(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                remove_adoption_schema(connection)
                connection.execute("DROP TABLE IF EXISTS provider_job_resolutions")
                connection.executescript(
                    """INSERT INTO topics
                       (project_id, chat_id, thread_id, title, created_at, updated_at)
                       VALUES ('example-project', -1001234567890, 7, 'Topic', 'now', 'now');
                       INSERT INTO agent_sessions
                       (session_id, topic_id, agent_id, generation, status, model, effort,
                        created_at, updated_at)
                       VALUES ('session', 1, 'codex', 1, 'active', 'model', 'high',
                               'now', 'now');
                       INSERT INTO provider_jobs
                       (job_id, idempotency_key, chat_id, message_id, topic_id,
                        topic_sequence, agent_id, session_id, session_generation, model,
                        effort, payload_text, status, error_class, error_code,
                        created_at, updated_at)
                       VALUES ('uncertain-job', 'resolution-key', -1001234567890, 1, 1, 1,
                               'codex', 'session', 1, 'model', 'high', 'hello',
                               'indeterminate', 'indeterminate', 'provider_outcome_unknown',
                               'now', 'now');
                       PRAGMA user_version = 22;"""
                )
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual(
                (result.previous_version, result.current_version),
                (22, LATEST_SCHEMA_VERSION),
            )
            migrated = sqlite3.connect(path)
            try:
                self.assertEqual(
                    migrated.execute(
                        "SELECT status, error_code FROM provider_jobs WHERE job_id = 'uncertain-job'"
                    ).fetchone(),
                    ("indeterminate", "provider_outcome_unknown"),
                )
                columns = {
                    row[1]
                    for row in migrated.execute("PRAGMA table_info(provider_job_resolutions)")
                }
                self.assertEqual(columns, {"job_id", "resolution", "resolved_at"})
                self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                migrated.close()

    def test_runtime_event_retention_migration_preserves_v20_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                remove_adoption_schema(connection)
                connection.execute("DROP INDEX runtime_events_retention")
                connection.execute(
                    "CREATE INDEX runtime_events_created_at ON runtime_events(created_at DESC)"
                )
                connection.execute(
                    """INSERT INTO runtime_events
                       (component, level, code, detail, created_at)
                       VALUES ('controller', 'warning', 'legacy', 'kept',
                               '2026-09-05T12:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO runtime_events
                       (component, level, code, detail, created_at)
                       VALUES ('controller', 'info', 'expired', 'removed',
                               '2020-01-01T00:00:00+00:00')"""
                )
                connection.executescript(
                    """INSERT INTO topics
                       (project_id, chat_id, thread_id, title, created_at, updated_at)
                       VALUES ('example-project', -1001234567890, 7, 'Topic', 'now', 'now');
                       INSERT INTO agent_sessions
                       (session_id, topic_id, agent_id, generation, status, model, effort,
                        created_at, updated_at)
                       VALUES ('session', 1, 'codex', 1, 'active', 'model', 'high',
                               'now', 'now');
                       INSERT INTO provider_jobs
                       (job_id, idempotency_key, chat_id, message_id, topic_id,
                        topic_sequence, agent_id, session_id, session_generation, model,
                        effort, payload_text, status, created_at, updated_at)
                       VALUES ('queued-job', 'retention-key', -1001234567890, 1, 1, 1,
                               'codex', 'session', 1, 'model', 'high', 'hello', 'queued',
                               'now', 'now');"""
                )
                connection.execute("PRAGMA user_version = 20")
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual(
                (result.previous_version, result.current_version), (20, LATEST_SCHEMA_VERSION)
            )
            migrated = sqlite3.connect(path)
            try:
                self.assertEqual(
                    migrated.execute("SELECT code FROM runtime_events").fetchall(),
                    [("legacy",)],
                )
                self.assertEqual(
                    migrated.execute(
                        "SELECT status, attempt_count FROM provider_jobs WHERE job_id = 'queued-job'"
                    ).fetchone(),
                    ("queued", 0),
                )
                columns = migrated.execute("PRAGMA index_info(runtime_events_retention)").fetchall()
                self.assertEqual([column[2] for column in columns], ["created_at", "event_id"])
                self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                migrated.close()

    def test_runtime_event_retention_migration_fault_restores_v20_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                remove_adoption_schema(connection)
                connection.execute("DROP INDEX runtime_events_retention")
                connection.execute("CREATE TABLE runtime_events_retention (marker TEXT NOT NULL)")
                connection.execute(
                    """INSERT INTO runtime_events
                       (component, level, code, detail, created_at)
                       VALUES ('controller', 'warning', 'legacy', 'kept',
                               '2026-09-05T12:00:00+00:00')"""
                )
                connection.execute("PRAGMA user_version = 20")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(sqlite3.OperationalError):
                migrate_database(path)

            restored = sqlite3.connect(path)
            try:
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 20)
                self.assertEqual(
                    restored.execute("SELECT code FROM runtime_events").fetchone()[0],
                    "legacy",
                )
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                restored.close()

    def test_transport_health_migration_preserves_v19_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                remove_adoption_schema(connection)
                connection.execute("DROP TABLE runtime_health")
                connection.executescript(MIGRATION_12)
                connection.executescript(MIGRATION_19)
                connection.execute(
                    """INSERT INTO runtime_health (
                           component, instance_id, pid, process_start_marker,
                           started_at, heartbeat_at, activity_state, provider_state,
                           release_version, release_git_sha, release_built_at,
                           release_clean, updated_at
                       ) VALUES ('sender', 'telegram-outbox-sender', 1234, 'old-start',
                                 '2026-09-04T12:00:00+00:00',
                                 '2026-09-04T12:00:00+00:00', 'idle', 'unknown',
                                 '0.6.0', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                                 '2026-09-04T11:00:00+00:00', 1,
                                 '2026-09-04T12:00:00+00:00')"""
                )
                connection.execute("PRAGMA user_version = 19")
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)
            self.assertEqual(
                (result.previous_version, result.current_version),
                (19, LATEST_SCHEMA_VERSION),
            )
            migrated = sqlite3.connect(path)
            try:
                row = migrated.execute(
                    """SELECT release_git_sha, transport_operation,
                              transport_failure_class, transport_status_code,
                              transport_retry_after, transport_consecutive_failures,
                              transport_success_at
                       FROM runtime_health WHERE component = 'sender'"""
                ).fetchone()
                self.assertEqual(row, ("a" * 40, None, None, None, None, 0, None))
                self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                migrated.close()

    def test_release_health_migration_preserves_v18_rows_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                remove_adoption_schema(connection)
                connection.execute("DROP TABLE runtime_health")
                connection.executescript(MIGRATION_12)
                connection.execute(
                    """INSERT INTO runtime_health (
                           component, instance_id, pid, process_start_marker,
                           started_at, heartbeat_at, activity_state, provider_state, updated_at
                       ) VALUES ('controller', 'project-hub-controller', 1234, 'old-start',
                                 '2026-09-04T12:00:00+00:00',
                                 '2026-09-04T12:00:00+00:00', 'idle', 'unknown',
                                 '2026-09-04T12:00:00+00:00')"""
                )
                connection.execute("PRAGMA user_version = 18")
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual(
                (result.previous_version, result.current_version), (18, LATEST_SCHEMA_VERSION)
            )
            migrated = sqlite3.connect(path)
            try:
                row = migrated.execute(
                    """SELECT release_version, release_git_sha, release_built_at, release_clean
                       FROM runtime_health
                       WHERE component = 'controller'"""
                ).fetchone()
                self.assertEqual(row, (None, None, None, 0))
                migrated.execute(
                    """INSERT INTO runtime_health (
                           component, instance_id, pid, process_start_marker,
                           started_at, heartbeat_at, activity_state, provider_state, updated_at
                       ) VALUES ('monitor', 'operations-monitor', 1234, 'monitor-start',
                                 '2026-09-04T12:00:00+00:00',
                                 '2026-09-04T12:00:00+00:00', 'idle', 'unknown',
                                 '2026-09-04T12:00:00+00:00')"""
                )
            finally:
                migrated.close()

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
                remove_adoption_schema(connection)
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
                    """DROP TABLE telegram_outbox_parts;
                       DROP TABLE telegram_outbox;
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
                remove_adoption_schema(connection)
                connection.executescript(
                    """DROP TABLE telegram_outbox_parts;
                       DROP TABLE provider_job_absorptions;
                       DROP TABLE provider_stop_requests;
                       PRAGMA user_version = 13;"""
                )
                connection.commit()
            finally:
                connection.close()

            result = migrate_database(path, create_backup=False)

            self.assertEqual(
                (result.previous_version, result.current_version),
                (13, LATEST_SCHEMA_VERSION),
            )
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
                self.assertIn("telegram_outbox_parts", tables)
            finally:
                migrated.close()

    def test_artifact_part_schema_rejects_incomplete_document_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            migrate_database(path, create_backup=False)
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    """INSERT INTO topics
                       (project_id, chat_id, thread_id, title, created_at, updated_at)
                       VALUES ('example-project', -1001234567890, 7, 'Topic', 'now', 'now');
                       INSERT INTO agent_sessions
                       (session_id, topic_id, agent_id, generation, status, model, effort,
                        created_at, updated_at)
                       VALUES ('session', 1, 'codex', 1, 'active', 'model', 'high',
                               'now', 'now');
                       INSERT INTO provider_jobs
                       (job_id, idempotency_key, chat_id, message_id, topic_id,
                        topic_sequence, agent_id, session_id, session_generation, model,
                        effort, payload_text, status, created_at, updated_at)
                       VALUES ('job', 'key', -1001234567890, 1, 1, 1, 'codex',
                               'session', 1, 'model', 'high', 'hello', 'result_ready',
                               'now', 'now');
                       INSERT INTO telegram_outbox
                       (outbox_id, job_id, sender_agent_id, chat_id, thread_id,
                        telegram_html, status, available_at, created_at, updated_at)
                       VALUES ('outbox', 'job', 'codex', -1001234567890, 7,
                               'done', 'pending', 'now', 'now', 'now');"""
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """INSERT INTO telegram_outbox_parts
                           (outbox_id, part_index, telegram_html, part_type, file_path,
                            file_name, file_size, file_sha256)
                           VALUES ('outbox', 1, 'file', 'document', '/tmp/file',
                                   'file.md', NULL, NULL)"""
                    )
            finally:
                connection.close()

    def test_latest_schema_rejects_pending_handoffs(self) -> None:
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
                with self.assertRaisesRegex(sqlite3.IntegrityError, "handoff is disabled"):
                    connection.execute(
                        """INSERT INTO pending_handoffs
                           (handoff_id, topic_id, target_agent_id, source_agent_id,
                            text, created_at)
                           VALUES ('handoff', 1, 'codex', 'hermes', 'context', 'now')"""
                    )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
