"""Additive preview prerequisite migration; existing authorities remain unchanged."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import HubState
from tests import test_outbox_sender as fixtures
from tests.schema_fixtures import legacy_delivery_hold_schema
from tests.test_delivery_certainty_migration import snapshot


class DeliveryControlMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 45):
            self.fixture.setUp()
            migrations.migrate_database(self.fixture.config.state_path, create_backup=False)
            self.addCleanup(self.fixture.tearDown)
            with (
                closing(sqlite3.connect(self.fixture.config.state_path)) as bridge,
                legacy_delivery_hold_schema(bridge),
            ):
                self.job_id = self.fixture.ready_outbox("opencode", 161)
        self.path = self.fixture.config.state_path
        with (
            patch.object(migrations, "LATEST_SCHEMA_VERSION", 45),
            closing(HubState.open(self.path, codex_permission_profile=None)) as state,
            legacy_delivery_hold_schema(state._connection),
        ):
            db = state._connection
            with db:
                db.execute("UPDATE topics SET execution_scope='root:/home/example/project'")
                db.execute("UPDATE telegram_outbox SET status='unknown'")
            outbox = state.get_telegram_outbox_for_job(self.job_id)
            token = state.preview_delivery_hold(outbox.outbox_id).snapshot
            state.release_delivery_hold(
                outbox.outbox_id, expected_snapshot=token, continue_without_confirmed_delivery=True
            )
            with db:
                db.execute(
                    """INSERT INTO telegram_outbox_parts
                       (outbox_id,part_index,telegram_html,part_type,file_path,file_name,file_size,file_sha256,
                        telegram_message_id,receipt_validation_version,delivered_at)
                       VALUES (?,2,'Example artifact','document','/home/example/spool/example.txt',
                               'example.txt',7,?,701,1,'2026-10-08T00:00:00+00:00')""",
                    (outbox.outbox_id, "b" * 64),
                )
                db.execute(
                    """INSERT INTO outcome_assessment_dispositions
                       (disposition_id,project_id,topic_id,chat_id,thread_id,input_message_id,
                        owner_user_id,reply_message_id,fingerprint_version,input_fingerprint,
                        disposition,refusal_code,created_at)
                       VALUES ('example-assessment','example-project',?,?,?,162,42,701,1,?,
                               'refused','example-refusal','2026-10-08T00:00:00+00:00')""",
                    (
                        state.get_provider_job(self.job_id).topic_id,
                        outbox.chat_id,
                        outbox.thread_id,
                        "c" * 64,
                    ),
                )
                db.execute(
                    """INSERT INTO provider_execution_checkpoints
                       (job_id,provider_thread_id,project_root,updated_at)
                       VALUES (?,'example-thread','/home/example/project','example-time')""",
                    (self.job_id,),
                )
                item = db.execute(
                    """INSERT INTO provider_visible_items
                       (job_id,item_id,phase,visible_text,created_at)
                       VALUES (?,'example-item','commentary','Example progress','example-time')""",
                    (self.job_id,),
                ).lastrowid
                db.execute(
                    """INSERT INTO provider_progress_deliveries
                       (progress_id,item_sequence,job_id,sender_agent_id,chat_id,thread_id,
                        telegram_html,status,available_at,created_at,updated_at)
                       VALUES ('example-progress',?,?,'opencode',?,?,'Example progress','unknown',
                               'example-time','example-time','example-time')""",
                    (item, self.job_id, outbox.chat_id, outbox.thread_id),
                )

    def test_populated_upgrade_preserves_rows_triggers_and_private_consistent_backup(self) -> None:
        with closing(sqlite3.connect(self.path)) as old:
            before = snapshot(old)
            objects = old.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 46):
            result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (45, 46))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 45)
        with closing(sqlite3.connect(self.path)) as upgraded:
            after = snapshot(upgraded)
            self.assertEqual(set(after) - set(before), {"telegram_delivery_control_dispositions"})
            self.assertEqual(after["telegram_delivery_control_dispositions"], [])
            for table in before:
                self.assertEqual(after[table], before[table], table)
            new_objects = upgraded.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            for obj in objects:
                self.assertIn(obj, new_objects)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 46):
            self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_ddl_fault_rolls_back_schema_version_rows_and_all_objects(self) -> None:
        with closing(sqlite3.connect(self.path)) as old:
            before = snapshot(old)
            objects = old.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        with (
            patch.object(
                migrations, "MIGRATION_46", migrations.MIGRATION_46 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(snapshot(old), before)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 45)
            self.assertEqual(
                old.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall(),
                objects,
            )
