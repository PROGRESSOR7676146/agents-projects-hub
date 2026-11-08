"""Schema56 preserves immutable local delivery consent while adding Hub provenance."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import HubState
from tests import test_outbox_sender as fixtures
from tests.schema_fixtures import project_historical_database


def snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Compare all retained rows, including WITHOUT ROWID current-schema tables."""
    result: dict[str, list[tuple[object, ...]]] = {}
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        order = ",".join(f'"{column}"' for column in columns)
        result[str(table)] = connection.execute(
            f'SELECT * FROM "{table}" ORDER BY {order}'
        ).fetchall()
    return result


def objects(connection: sqlite3.Connection) -> dict[str, tuple[str, str, str | None]]:
    return {
        str(row[1]): (str(row[0]), str(row[1]), row[2])
        for row in connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")
    }


class TelegramDeliveryHoldMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        job_id = self.fixture.ready_outbox("opencode", 561)
        source = self.fixture.config.state_path
        with closing(HubState.open_existing(source)) as state:
            with state._connection:
                state._connection.execute(
                    "UPDATE topics SET execution_scope='root:/home/example/project'"
                )
                state._connection.execute(
                    "UPDATE telegram_outbox SET status='unknown',error_code='fictional_timeout' "
                    "WHERE job_id=?",
                    (job_id,),
                )
            outbox = state.get_telegram_outbox_for_job(job_id)
            preview = state.preview_delivery_hold(outbox.outbox_id)
            state.release_delivery_hold(
                outbox.outbox_id,
                expected_snapshot=preview.snapshot,
                continue_without_confirmed_delivery=True,
            )
        self.path = source.with_name("example-schema55.db")
        project_historical_database(source, self.path, 55)
        with closing(sqlite3.connect(self.path)) as historical:
            self.assertEqual(historical.execute("PRAGMA user_version").fetchone()[0], 55)
            self.assertEqual(
                historical.execute(
                    "SELECT COUNT(*) FROM telegram_delivery_hold_dispositions"
                ).fetchone()[0],
                1,
            )

    def test_populated_schema55_upgrade_keeps_legacy_rows_fk_and_immutable_objects(self) -> None:
        with closing(sqlite3.connect(self.path)) as old:
            before = snapshot(old)
            old_objects = objects(old)
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (55, 56))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(snapshot(backup), before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 55)
        with closing(sqlite3.connect(self.path)) as upgraded:
            after = snapshot(upgraded)
            self.assertEqual(after["telegram_delivery_hold_callback_tokens"], [])
            self.assertEqual(after["telegram_delivery_hold_remote_dispositions"], [])
            for table, rows in before.items():
                self.assertEqual(after[table], rows, table)
            new_objects = objects(upgraded)
            for name in (
                "telegram_delivery_hold_dispositions",
                "telegram_delivery_hold_dispositions_no_update",
                "telegram_delivery_hold_dispositions_no_delete",
                "telegram_delivery_hold_topic_binding_guard",
            ):
                self.assertEqual(new_objects[name], old_objects[name], name)
            self.assertIn("telegram_delivery_hold_dispositions_no_remote_conflict", new_objects)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_schema56_ddl_fault_rolls_back_version_rows_and_legacy_objects(self) -> None:
        with closing(sqlite3.connect(self.path)) as old:
            before = snapshot(old)
            old_objects = objects(old)
        with (
            patch.object(
                migrations,
                "MIGRATION_56",
                migrations.MIGRATION_56 + "\nINVALID EXAMPLE SQL;",
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as retained:
            self.assertEqual(snapshot(retained), before)
            self.assertEqual(objects(retained), old_objects)
            self.assertEqual(retained.execute("PRAGMA user_version").fetchone()[0], 55)


if __name__ == "__main__":
    unittest.main()
