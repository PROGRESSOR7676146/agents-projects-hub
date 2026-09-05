from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_codex_router.migrations import backup_database, migrate_database


def _prepare_production_shaped_v20(path: Path, *, migration_fault: bool = False) -> None:
    migrate_database(path, create_backup=False)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX runtime_events_retention")
        connection.execute(
            "CREATE INDEX runtime_events_created_at ON runtime_events(created_at DESC)"
        )
        if migration_fault:
            connection.execute("CREATE TABLE runtime_events_retention (marker TEXT NOT NULL)")
        connection.executescript(
            """
            INSERT INTO topics
                (project_id, chat_id, thread_id, title, active_agent_id, created_at, updated_at)
            VALUES
                ('example-project', -1001234567890, 7, 'Release rehearsal', 'codex',
                 '2026-09-05T10:00:00+00:00', '2026-09-05T10:00:00+00:00');
            INSERT INTO agent_sessions
                (session_id, topic_id, agent_id, generation, status, model, effort,
                 provider_session_id, writer_mode, created_at, updated_at)
            VALUES
                ('active-session', 1, 'codex', 1, 'active', 'example-model', 'high',
                 'provider-active', 'telegram', '2026-09-05T10:00:00+00:00',
                 '2026-09-05T10:00:00+00:00'),
                ('satellite-session', 1, 'opencode', 1, 'satellite', 'example-model', 'medium',
                 'provider-satellite', 'telegram', '2026-09-05T10:00:00+00:00',
                 '2026-09-05T10:00:00+00:00');
            INSERT INTO provider_jobs
                (job_id, idempotency_key, chat_id, message_id, topic_id, topic_sequence,
                 agent_id, session_id, session_generation, provider_session_id, model, effort,
                 payload_text, status, attempt_count, provider_started_at, error_class,
                 error_code, error_detail, created_at, updated_at)
            VALUES
                ('queued-job', 'queue-key', -1001234567890, 101, 1, 1, 'codex',
                 'active-session', 1, 'provider-active', 'example-model', 'high',
                 'queued payload', 'queued', 0, NULL, NULL, NULL, NULL,
                 '2026-09-05T10:01:00+00:00', '2026-09-05T10:01:00+00:00'),
                ('result-job', 'result-key', -1001234567890, 102, 1, 2, 'codex',
                 'active-session', 1, 'provider-active', 'example-model', 'high',
                 'result payload', 'result_ready', 1, '2026-09-05T10:02:00+00:00',
                 NULL, NULL, NULL, '2026-09-05T10:02:00+00:00',
                 '2026-09-05T10:02:00+00:00'),
                ('indeterminate-job', 'indeterminate-key', -1001234567890, 103, 1, 3,
                 'codex', 'active-session', 1, 'provider-active', 'example-model', 'high',
                 'ambiguous payload', 'indeterminate', 1, '2026-09-05T10:03:00+00:00',
                 'indeterminate', 'provider_outcome_unknown', 'acceptance was not proven',
                 '2026-09-05T10:03:00+00:00', '2026-09-05T10:03:00+00:00');
            INSERT INTO provider_job_results
                (result_id, job_id, visible_response, provider_session_id, actual_model, created_at)
            VALUES
                ('result', 'result-job', 'durable response', 'provider-active',
                 'example-model', '2026-09-05T10:04:00+00:00');
            INSERT INTO telegram_outbox
                (outbox_id, job_id, sender_agent_id, chat_id, thread_id, telegram_html,
                 status, attempt_count, available_at, created_at, updated_at)
            VALUES
                ('outbox', 'result-job', 'codex', -1001234567890, 7, 'durable response',
                 'pending', 2, '2026-09-05T10:04:00+00:00',
                 '2026-09-05T10:04:00+00:00', '2026-09-05T10:04:00+00:00');
            INSERT INTO telegram_outbox_parts
                (outbox_id, part_index, telegram_html, part_type)
            VALUES ('outbox', 1, 'durable response', 'text');
            INSERT INTO runtime_events (component, level, code, detail, created_at)
            VALUES
                ('controller', 'info', 'expired', 'eligible for retention',
                 '2020-01-01T00:00:00+00:00'),
                ('controller', 'warning', 'recent', 'must survive',
                 '2999-01-01T00:00:00+00:00');
            PRAGMA user_version = 20;
            """
        )
        connection.commit()
    finally:
        connection.close()


def _durable_work_snapshot(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        jobs = connection.execute(
            """SELECT job_id, status, attempt_count, error_class, error_code
               FROM provider_jobs ORDER BY topic_sequence"""
        ).fetchall()
        outbox = connection.execute(
            """SELECT telegram_outbox.outbox_id, telegram_outbox.status,
                      telegram_outbox.attempt_count, telegram_outbox_parts.telegram_html
               FROM telegram_outbox JOIN telegram_outbox_parts USING (outbox_id)"""
        ).fetchall()
        return jobs, outbox
    finally:
        connection.close()


class Schema2021RehearsalTests(unittest.TestCase):
    def test_migration_and_backup_rollback_preserve_durable_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "production-copy.db"
            _prepare_production_shaped_v20(state_path)
            expected = _durable_work_snapshot(state_path)

            result = migrate_database(state_path)

            self.assertEqual((result.previous_version, result.current_version), (20, 21))
            self.assertEqual(_durable_work_snapshot(state_path), expected)
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertEqual(_durable_work_snapshot(result.backup_path), expected)
            rollback_path = Path(directory) / "rollback-copy.db"
            backup_database(result.backup_path, rollback_path)
            rollback = sqlite3.connect(rollback_path)
            try:
                self.assertEqual(rollback.execute("PRAGMA user_version").fetchone()[0], 20)
                self.assertEqual(rollback.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                rollback.close()
            self.assertEqual(_durable_work_snapshot(rollback_path), expected)

            migrated = sqlite3.connect(state_path)
            try:
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 21)
                self.assertEqual(
                    migrated.execute(
                        "SELECT code FROM runtime_events ORDER BY event_id"
                    ).fetchall(),
                    [("recent",)],
                )
                self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                migrated.close()

    def test_concurrent_committed_write_survives_migration_after_backup_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "production-copy.db"
            _prepare_production_shaped_v20(state_path)
            writer = sqlite3.connect(state_path)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                """INSERT INTO runtime_events (component, level, code, detail, created_at)
                   VALUES ('sender', 'warning', 'concurrent', 'committed before migration lock',
                           '2999-01-02T00:00:00+00:00')"""
            )
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(migrate_database, state_path)
                    time.sleep(0.05)
                    self.assertFalse(future.done())
                    writer.commit()
                    result = future.result(timeout=5)
            finally:
                writer.close()

            self.assertEqual((result.previous_version, result.current_version), (20, 21))
            migrated = sqlite3.connect(state_path)
            try:
                self.assertEqual(
                    migrated.execute(
                        "SELECT COUNT(*) FROM runtime_events WHERE code = 'concurrent'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                migrated.close()

    def test_migration_fault_rolls_back_without_losing_concurrent_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "production-copy.db"
            _prepare_production_shaped_v20(state_path, migration_fault=True)
            expected = _durable_work_snapshot(state_path)
            writer = sqlite3.connect(state_path)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                """INSERT INTO runtime_events (component, level, code, detail, created_at)
                   VALUES ('controller', 'warning', 'concurrent', 'must not be restored away',
                           '2999-01-02T00:00:00+00:00')"""
            )
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(migrate_database, state_path)
                    time.sleep(0.05)
                    self.assertFalse(future.done())
                    writer.commit()
                    with self.assertRaises(sqlite3.OperationalError):
                        future.result(timeout=5)
            finally:
                writer.close()

            self.assertEqual(_durable_work_snapshot(state_path), expected)
            restored = sqlite3.connect(state_path)
            try:
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 20)
                self.assertIsNotNone(
                    restored.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'index' AND name = 'runtime_events_created_at'"""
                    ).fetchone()
                )
                self.assertEqual(
                    restored.execute(
                        "SELECT COUNT(*) FROM runtime_events WHERE code = 'concurrent'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
