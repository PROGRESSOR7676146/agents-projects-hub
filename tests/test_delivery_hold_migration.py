"""Populated schema43 to additive schema44; no provider, Telegram or service calls."""

from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.cli import main
from hermes_codex_router.state import HubState, StateError
from tests import test_outbox_sender as fixtures
from tests.test_delivery_certainty_migration import snapshot


class DeliveryHoldMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 44):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 44):
            self.job_id = self.fixture.ready_outbox("opencode", 91)
        self.path = self.fixture.config.state_path
        with closing(sqlite3.connect(self.path)) as db, db:
            self.outbox_id = db.execute(
                "SELECT outbox_id FROM telegram_outbox WHERE job_id=?", (self.job_id,)
            ).fetchone()[0]
            db.execute(
                "UPDATE telegram_outbox SET status='unknown',error_code='example_unknown' WHERE outbox_id=?",
                (self.outbox_id,),
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM telegram_delivery_hold_dispositions").fetchone()[
                    0
                ],
                0,
            )
            db.execute("UPDATE topics SET execution_scope=?", ("root:" + str(self.fixture.base),))
            db.execute("DROP TRIGGER telegram_delivery_hold_topic_binding_guard")
            db.execute("DROP TABLE telegram_delivery_hold_dispositions")
            db.execute("PRAGMA user_version=43")

    def test_populated_additive_upgrade_consistent_private_backup_and_idempotence(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = snapshot(old)
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 44):
            result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (43, 44))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 43)
        with sqlite3.connect(self.path) as upgraded:
            after = snapshot(upgraded)
            self.assertEqual(set(after) - set(before), {"telegram_delivery_hold_dispositions"})
            self.assertEqual(after["telegram_delivery_hold_dispositions"], [])
            for table in before:
                self.assertEqual(after[table], before[table], table)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 44):
            self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_ddl_fault_restores_version_rows_and_objects(self) -> None:
        with sqlite3.connect(self.path) as old:
            before = snapshot(old)
            objects = old.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        with (
            patch.object(
                migrations, "MIGRATION_44", migrations.MIGRATION_44 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with sqlite3.connect(self.path) as old:
            self.assertEqual(snapshot(old), before)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 43)
            self.assertEqual(
                old.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall(),
                objects,
            )

    def test_preview_apply_cli_refuse_old_schema_without_implicit_migration(self) -> None:
        # Closing a writable SQLite connection may checkpoint existing WAL;
        # assert logical state/schema, rather than physical journal placement.
        with sqlite3.connect(self.path) as old:
            before = snapshot(old)
            objects = old.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["delivery-hold", str(self.path), self.outbox_id]), 2)
            self.assertEqual(
                main(
                    [
                        "delivery-hold",
                        str(self.path),
                        self.outbox_id,
                        "--apply",
                        "--snapshot",
                        "0" * 64,
                        "--continue-without-confirmed-delivery",
                    ]
                ),
                2,
            )
        with sqlite3.connect(self.path) as retained:
            self.assertEqual(snapshot(retained), before)
            self.assertEqual(retained.execute("PRAGMA user_version").fetchone()[0], 43)
            self.assertEqual(
                retained.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY name"
                ).fetchall(),
                objects,
            )
        self.assertEqual(list(self.path.parent.glob("*.backup*")), [])
        with self.assertRaises(StateError):
            HubState.open_read_only(self.path)

    def test_cli_preview_apply_and_dedup_without_configuration_or_credentials(self) -> None:
        import json

        migrations.migrate_database(self.path)
        with patch(
            "hermes_codex_router.cli.load_hub_config",
            side_effect=AssertionError("no configuration"),
        ) as config:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["delivery-hold", str(self.path), self.outbox_id]), 0)
            preview = json.loads(output.getvalue())
            self.assertEqual(preview["delivery_status"], "unknown")
            self.assertNotIn("durable task", output.getvalue())
            self.assertNotIn("opencode result", output.getvalue())
            args = [
                "delivery-hold",
                str(self.path),
                self.outbox_id,
                "--apply",
                "--snapshot",
                preview["snapshot"],
                "--continue-without-confirmed-delivery",
            ]
            outputs = []
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(args), 0)
                outputs.append(json.loads(output.getvalue()))
            self.assertEqual(outputs[0], outputs[1])
            self.assertFalse(outputs[0]["automatic_resend"])
            self.assertFalse(outputs[0]["productive_replay_authorized"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["delivery-hold", str(self.path), self.outbox_id]), 0)
            released = json.loads(output.getvalue())
            self.assertIsNone(released["action"])
            self.assertIn("Already released", released["effect"])
            with closing(sqlite3.connect(self.path)) as damaged, damaged:
                damaged.execute("DROP TRIGGER telegram_delivery_hold_topic_binding_guard")
                damaged.execute("UPDATE topics SET execution_scope='root:/home/example/changed'")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(args), 0)
            historical = json.loads(output.getvalue())
            self.assertEqual(historical["hold_status"], "disposition_binding_changed")
            self.assertIn("topic hold remains", historical["effect"])
            self.assertNotIn("may proceed", historical["effect"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["delivery-hold", str(self.path), self.outbox_id]), 0)
            changed = json.loads(output.getvalue())
            self.assertIsNone(changed["action"])
            self.assertIn("hold remains", changed["effect"])
            self.assertIn("no new decision can replace it", changed["effect"])
            config.assert_not_called()

    def test_missing_state_is_never_created_and_apply_requires_explicit_controls(self) -> None:
        missing = self.path.parent / "missing.db"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["delivery-hold", str(missing), self.outbox_id]), 2)
            self.assertEqual(main(["delivery-hold", str(missing), self.outbox_id, "--apply"]), 2)
        self.assertFalse(missing.exists())
