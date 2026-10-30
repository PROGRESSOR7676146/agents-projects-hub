"""Schema53 evidence remains legacy; migration, backup and failure are atomic."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import HubState
from tests import test_outbox_sender as fixtures
from tests.schema_fixtures import project_historical_database


class DeliveryReceiptMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job = self.fixture.ready_outbox("opencode", 81)
        self.path = self.fixture.config.state_path.with_name("historical.db")
        project_historical_database(self.fixture.config.state_path, self.path, 53)

    def test_populated_upgrade_preserves_evidence_and_private_backup(self) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            before = db.execute("SELECT * FROM telegram_outbox").fetchall()
            parts = db.execute("SELECT * FROM telegram_outbox_parts").fetchall()
        result = migrations.migrate_database(self.path)
        self.assertEqual(
            (result.previous_version, result.current_version),
            (53, migrations.LATEST_SCHEMA_VERSION),
        )
        assert result.backup_path
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(backup.execute("SELECT * FROM telegram_outbox").fetchall(), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 53)
        with closing(HubState.open_existing(self.path)) as state:
            after = state._connection.execute("SELECT * FROM telegram_outbox").fetchall()
            self.assertEqual([tuple(r)[:-1] for r in after], before)
            self.assertEqual(after[0]["purpose"], "legacy")
            self.assertEqual(
                state._connection.execute("SELECT * FROM telegram_send_attempts").fetchall(), []
            )
            self.assertEqual(
                [
                    tuple(r)
                    for r in state._connection.execute("SELECT * FROM telegram_outbox_parts")
                ],
                parts,
            )
            self.assertEqual(state._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_ddl_fault_rolls_back_column_and_version(self) -> None:
        original = migrations._execute_migration_script

        def fault(db: sqlite3.Connection, script: str) -> None:
            original(db, script)
            if script == migrations.MIGRATION_54:
                raise sqlite3.OperationalError("injected after schema54 DDL")

        with patch.object(migrations, "_execute_migration_script", side_effect=fault):
            with self.assertRaises(sqlite3.OperationalError):
                migrations.migrate_database(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 53)
            self.assertNotIn(
                "purpose", [r[1] for r in db.execute("PRAGMA table_info(telegram_outbox)")]
            )
            self.assertEqual(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE name='telegram_send_attempts'"
                ).fetchall(),
                [],
            )
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_legacy_resultless_notice_remains_a_delivery_blocker(self) -> None:
        # Construct historical evidence before migration; absence of result must
        # never be used to upgrade an ambiguous old record into a new notice.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM provider_job_results WHERE job_id=?", (self.job,))
            db.execute("UPDATE provider_jobs SET status='failed' WHERE job_id=?", (self.job,))
            db.execute("UPDATE telegram_outbox SET status='unknown' WHERE job_id=?", (self.job,))
        migrations.migrate_database(self.path)
        with closing(HubState.open_existing(self.path)) as state:
            counts = state.delivery.uncertain_counts()
            self.assertEqual(counts["unknown_delivery"], 1)
            self.assertEqual(counts["outstanding_delivery_holds"], 1)
            self.assertEqual(counts["nonblocking_notice_delivery"], 0)


if __name__ == "__main__":
    unittest.main()
