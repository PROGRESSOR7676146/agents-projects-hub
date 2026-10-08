"""Populated schema46 activation preserves evidence and rolls back DDL faults."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import HubState
from tests import test_delivery_control_preview as fixtures
from tests.test_delivery_certainty_migration import snapshot


def objects(db: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row) for row in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")
    ]


class DeliveryControlActivationMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.DeliveryControlPreviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.fixture.config.state_path
        db = self.fixture.db
        old = self.fixture.state.preview_delivery_hold(self.fixture.outbox.outbox_id)
        self.fixture.state.release_delivery_hold(
            self.fixture.outbox.outbox_id,
            expected_snapshot=old.snapshot,
            continue_without_confirmed_delivery=True,
        )
        # A storage-only row, as supported by schema46. Its old topic guard is
        # restored literally; this is a historical fixture, never a downgrade.
        self.fixture.seed()
        self.fixture.progress()
        with db:
            db.execute("DROP TRIGGER telegram_delivery_hold_topic_binding_guard")
            historical_guard = migrations.MIGRATION_44[
                migrations.MIGRATION_44.index(
                    "CREATE TRIGGER telegram_delivery_hold_topic_binding_guard"
                ) :
            ]
            migrations._execute_migration_script(db, historical_guard)
            db.execute("PRAGMA user_version=46")
        self.fixture.state.close()

    def test_upgrade_preserves_rows_and_backup_then_enables_exact_trigger_exception(self) -> None:
        with closing(sqlite3.connect(self.path)) as old:
            before = snapshot(old)
            old_objects = objects(old)
            with self.assertRaises(sqlite3.IntegrityError), old:
                old.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (46, 47))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(snapshot(backup), before)
            self.assertEqual(objects(backup), old_objects)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 46)
        with closing(sqlite3.connect(self.path)) as upgraded:
            self.assertEqual(snapshot(upgraded), before)
            changed = {obj[1] for obj in objects(upgraded)}
            self.assertEqual(changed, {obj[1] for obj in old_objects})
            for obj in old_objects:
                if obj[1] != "telegram_delivery_hold_topic_binding_guard":
                    self.assertIn(obj, objects(upgraded))
            with upgraded:
                upgraded.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
            with self.assertRaises(sqlite3.IntegrityError), upgraded:
                upgraded.execute("UPDATE topics SET thread_id=999")
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_ddl_fault_preserves_every_row_object_and_version(self) -> None:
        with closing(sqlite3.connect(self.path)) as old:
            before = snapshot(old)
            old_objects = objects(old)
        with (
            patch.object(
                migrations, "MIGRATION_47", migrations.MIGRATION_47 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(snapshot(old), before)
            self.assertEqual(objects(old), old_objects)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 46)

    def test_upgraded_and_fresh_schema_have_identical_objects(self) -> None:
        migrations.migrate_database(self.path)
        fresh = self.path.parent / "example-fresh.db"
        with closing(HubState.open(fresh, codex_permission_profile=None)) as state:
            new_objects = objects(state._connection)
        with closing(sqlite3.connect(self.path)) as upgraded:
            self.assertEqual(objects(upgraded), new_objects)


if __name__ == "__main__":
    unittest.main()
