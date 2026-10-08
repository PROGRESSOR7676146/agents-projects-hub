"""Genuine populated schema47 upgrades never manufacture historical send authority."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.codex_turn_controls import CodexTurnControls
from hermes_codex_router.state import HubState
from tests import test_codex_turn_controls as fixtures
from tests.test_delivery_control_activation_migration import objects


def columns(db):
    return {
        table: tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
        for (table,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def rows(db, table_columns):
    return {
        table: db.execute(
            f'SELECT {",".join(chr(34) + name + chr(34) for name in names)} FROM "{table}" ORDER BY rowid'
        ).fetchall()
        for table, names in table_columns.items()
    }


class CodexControlMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexTurnControlJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.journal.record_turn(self.fixture.job_id, self.fixture.token, "example-turn")
        job = self.fixture.state.get_provider_job(self.fixture.job_id)
        self.fixture.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=999, target_agent_id="codex"
        )
        self.path = self.fixture.fixture.config.state_path.with_name("example-historical47.db")
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 47):
            migrations.migrate_database(self.path, create_backup=False)
        # Copy the fictional historical columns into a separately-created genuine
        # old schema. The original v48 fixture and its authority stay intact.
        with closing(sqlite3.connect(self.path)) as old:
            self.old_columns = columns(old)
            for table, names in self.old_columns.items():
                source = self.fixture.state._connection.execute(
                    f'SELECT {",".join(chr(34) + name + chr(34) for name in names)} FROM "{table}"'
                ).fetchall()
                if source:
                    old.executemany(
                        f'INSERT INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
                        [tuple(row) for row in source],
                    )
            old.commit()
            self.before = rows(old, self.old_columns)
            self.old_objects = objects(old)

    def test_upgrade_preserves_every_historical_column_and_consistent_private_backup(self) -> None:
        result = migrations.migrate_database(self.path)
        self.assertEqual(
            (result.previous_version, result.current_version),
            (47, migrations.LATEST_SCHEMA_VERSION),
        )
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(rows(backup, self.old_columns), self.before)
            self.assertEqual(objects(backup), self.old_objects)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 47)
        with closing(sqlite3.connect(self.path)) as upgraded:
            self.assertEqual(rows(upgraded, self.old_columns), self.before)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_backfilled_target_is_read_only_with_unknown_acceptance_time(self) -> None:
        migrations.migrate_database(self.path)
        with closing(HubState.open(self.path, codex_permission_profile=None)) as state:
            controls = CodexTurnControls(
                state._connection, transaction=state._immediate_transaction
            )
            row = controls.read(self.fixture.job_id)
            assert row is not None
            self.assertEqual(row["origin"], "legacy_read_only")
            self.assertIsNone(row["accepted_at"])
            with self.assertRaises(sqlite3.IntegrityError), state._immediate_transaction():
                state._connection.execute(
                    """UPDATE codex_turn_controls SET send_owner_token_hash='example-owner',
                       send_started_at='example-time',interrupt_source='late' WHERE job_id=?""",
                    (self.fixture.job_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError), state._immediate_transaction():
                state._connection.execute(
                    "UPDATE codex_turn_controls SET origin='accepted_v48' WHERE job_id=?",
                    (self.fixture.job_id,),
                )
            with state._immediate_transaction():
                controls.bind_covering_stops_in_transaction()
            retained = controls.read(self.fixture.job_id)
            assert retained is not None
            self.assertIsNotNone(retained["stop_request_id"])

    def test_ddl_fault_rolls_back_objects_all_rows_and_schema(self) -> None:
        with (
            patch.object(
                migrations, "MIGRATION_48", migrations.MIGRATION_48 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(rows(old, self.old_columns), self.before)
            self.assertEqual(objects(old), self.old_objects)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 47)

    def test_fresh_and_migrated_schema_match_exactly(self) -> None:
        migrations.migrate_database(self.path)
        with closing(sqlite3.connect(self.path)) as upgraded:
            self.assertEqual(objects(upgraded), objects(self.fixture.state._connection))


if __name__ == "__main__":
    unittest.main()
