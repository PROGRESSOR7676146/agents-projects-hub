"""Populated schema-42 rebuild, foreign keys, trigger preservation and DDL rollback."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

from hermes_codex_router import migrations
from tests import test_outbox_sender as outbox_fixtures
from tests import test_progress_delivery as progress_fixtures
from tests.schema_fixtures import legacy_delivery_hold_schema


def snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        row[0]: connection.execute(f'SELECT * FROM "{row[0]}" ORDER BY rowid').fetchall()
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


class DeliveryCertaintyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(migrations, "LATEST_SCHEMA_VERSION", 43))
        progress = progress_fixtures.DurableProgressDeliveryTests()
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 42):
            progress.setUp()
        self.addCleanup(progress.tearDown)
        self.path = progress.config.state_path
        with closing(sqlite3.connect(self.path)) as bridge, legacy_delivery_hold_schema(bridge):
            job_id, token, journal = progress.executing_job()
            journal.record_item(job_id, token, "example-progress", "Example progress", "commentary")
            progress.state.commit_provider_result(
                job_id,
                token,
                visible_response="Example",
                sender_agent_id="codex",
                telegram_html="Example",
            )
            progress.state.close()
            fixture = outbox_fixtures.TelegramOutboxSenderTests()
            fixture.setUp()
            self.addCleanup(fixture.tearDown)
            fixture.config = replace(fixture.config, state_path=self.path)
            with patch.object(migrations, "LATEST_SCHEMA_VERSION", 42):
                self.sending_job = fixture.ready_outbox("opencode", 81)
                self.delivered_job = fixture.ready_outbox("opencode", 82)
                self.failed_job = fixture.ready_outbox("opencode", 83)
        with sqlite3.connect(self.path) as old:
            old.execute(
                """UPDATE telegram_outbox SET status='sending', lease_owner='example-sender',
                   lease_token='example-token', lease_expires_at='2026-01-01T00:00:00+00:00'
                   WHERE job_id=?""",
                (self.sending_job,),
            )
            old.execute(
                "UPDATE telegram_outbox SET status='delivered', telegram_message_id=101 WHERE job_id=?",
                (self.delivered_job,),
            )
            old.execute(
                """UPDATE telegram_outbox_parts SET telegram_message_id=101
                   WHERE outbox_id=(SELECT outbox_id FROM telegram_outbox WHERE job_id=?)""",
                (self.delivered_job,),
            )
            old.execute(
                "UPDATE telegram_outbox SET status='failed' WHERE job_id=?", (self.failed_job,)
            )
            old.execute(
                """INSERT INTO telegram_outbox_parts
                   (outbox_id,part_index,telegram_html,part_type,file_path,file_name,file_size,file_sha256)
                   SELECT outbox_id,2,'Example artifact','document',
                          '/home/example/spool/example.md','example.md',7,?
                   FROM telegram_outbox WHERE job_id=?""",
                ("a" * 64, self.sending_job),
            )
            old.execute(
                """UPDATE provider_progress_deliveries SET status='sending', lease_owner='example-sender',
                   lease_token='example-progress-token', lease_expires_at='2026-01-01T00:00:00+00:00'"""
            )
            old.execute(
                """INSERT INTO provider_recovery_notices
                   (job_id,outbox_id,telegram_html,delivery_status,telegram_message_id,saved_at)
                   VALUES (?,'example-historical-outbox','Example historical notice','delivered',91,
                           '2026-01-01T00:00:00+00:00')""",
                (self.delivered_job,),
            )

    def test_upgrade_preserves_parts_jobs_receipts_and_backup_without_invented_provenance(
        self,
    ) -> None:
        with sqlite3.connect(self.path) as old:
            before = snapshot(old)
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (42, 43))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 42)
        with sqlite3.connect(self.path) as upgraded:
            after = snapshot(upgraded)
            self.assertEqual(after["provider_recovery_notice_parts"], [])
            for table, rows in before.items():
                if table not in (
                    "telegram_outbox",
                    "telegram_outbox_parts",
                    "provider_progress_deliveries",
                ):
                    self.assertEqual(after[table], rows, table)
            self.assertEqual(
                after["telegram_outbox_parts"],
                [(*row, 0) for row in before["telegram_outbox_parts"]],
            )
            upgraded.row_factory = sqlite3.Row
            for table in ("telegram_outbox", "provider_progress_deliveries"):
                for row in upgraded.execute(f"SELECT * FROM {table}"):
                    self.assertIsNone(row["send_started_at"])
                    self.assertIsNone(row["lease_token"])
                    if row["error_code"] == "legacy_send_attempt_unknown":
                        self.assertEqual(row["status"], "unknown")
                columns = [row[1] for row in upgraded.execute(f"PRAGMA table_info({table})")][:-1]
                for legacy, migrated in zip(before[table], after[table], strict=True):
                    expected = dict(zip(columns, legacy, strict=True))
                    attempted = expected["status"] == "sending"
                    if attempted:
                        expected["status"] = "unknown"
                        expected["error_code"] = "legacy_send_attempt_unknown"
                    for field in ("lease_owner", "lease_token", "lease_expires_at"):
                        expected[field] = None
                    self.assertEqual(migrated, (*expected.values(), None), table)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            objects = {row[0] for row in upgraded.execute("SELECT name FROM sqlite_master")}
            self.assertTrue(
                {
                    "telegram_outbox_sender_ready",
                    "telegram_outbox_stale_lease",
                    "telegram_outbox_parts_artifact_insert",
                    "telegram_outbox_parts_artifact_update",
                    "provider_progress_delivery_ready",
                    "provider_progress_delivery_job",
                }
                <= objects
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid telegram outbox part"):
                upgraded.execute(
                    "UPDATE telegram_outbox_parts SET file_size=NULL WHERE part_type='document'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid telegram outbox part"):
                upgraded.execute(
                    "INSERT INTO telegram_outbox_parts (outbox_id,part_index,telegram_html,part_type) SELECT outbox_id,3,'Example','document' FROM telegram_outbox LIMIT 1"
                )
            self.assertEqual(
                upgraded.execute(
                    "SELECT status FROM telegram_outbox WHERE job_id=?", (self.sending_job,)
                ).fetchone()[0],
                "unknown",
            )
        repeated = migrations.migrate_database(self.path)
        self.assertEqual((repeated.previous_version, repeated.current_version), (43, 43))
        self.assertIsNone(repeated.backup_path)

    def test_fault_after_old_child_drop_rolls_back_everything(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = snapshot(old)
            before_objects = old.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        script = migrations.MIGRATION_43.replace(
            "DROP TABLE telegram_outbox_parts;",
            "DROP TABLE telegram_outbox_parts;\nINVALID EXAMPLE SQL;",
        )
        with patch.object(migrations, "MIGRATION_43", script), self.assertRaises(sqlite3.Error):
            migrations.migrate_database(self.path, create_backup=False)
        with sqlite3.connect(self.path) as retained:
            self.assertEqual(snapshot(retained), before)
            self.assertEqual(
                retained.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY name"
                ).fetchall(),
                before_objects,
            )
            self.assertEqual(retained.execute("PRAGMA user_version").fetchone()[0], 42)

    def test_foreign_key_check_rejects_deferred_invalid_copy_and_rolls_back(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = snapshot(old)
        original = migrations._execute_migration_script

        def inject(connection: sqlite3.Connection, script: str) -> None:
            original(connection, script)
            if script == migrations.MIGRATION_43:
                connection.execute("PRAGMA defer_foreign_keys=ON")
                connection.execute(
                    "UPDATE telegram_outbox SET job_id='example-missing-job' WHERE job_id=?",
                    (self.failed_job,),
                )

        with patch.object(migrations, "_execute_migration_script", side_effect=inject):
            with self.assertRaisesRegex(RuntimeError, "foreign key check"):
                migrations.migrate_database(self.path, create_backup=False)
        with sqlite3.connect(self.path) as retained:
            self.assertEqual(snapshot(retained), before)
            self.assertEqual(retained.execute("PRAGMA user_version").fetchone()[0], 42)
