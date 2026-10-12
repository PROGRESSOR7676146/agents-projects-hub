"""Genuine schema50 upgrade preserves unknown control and all old evidence."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import HubState
from tests import test_telegram_turn_provenance as fixtures
from tests.schema_fixtures import project_historical_database
from tests.test_codex_control_migration import columns, rows
from tests.test_delivery_control_activation_migration import objects


class IngressAssessmentMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramTurnProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        job, _ = self.fixture.enqueue("hub")
        self.fixture.accept(job)
        self.job_id = job.job_id
        state = self.fixture.state
        control = state.codex_controls.read(self.job_id)
        assert control is not None
        self.now = now = datetime.fromisoformat(control["accepted_at"])
        self.owner = owner = state.telegram_ingress.register(
            "hub", instance_token="example-historical-publisher", previous_epoch=0, now=now
        )
        for sequence in (1, 2, 3):
            self.assertTrue(
                state.telegram_ingress.record_poll(
                    owner, sequence=sequence, succeeded=False, observed_at=now
                )
            )
        with state._immediate_transaction():
            cursor = state._connection.execute(
                """UPDATE codex_turn_controls SET send_owner_token_hash=?,send_started_at=?,
                interrupt_source='protective',interrupt_outcome='unknown' WHERE job_id=?""",
                ("a" * 64, now.isoformat(), self.job_id),
            )
            self.assertEqual(cursor.rowcount, 1)
        self.path = self.fixture.harness.config.state_path.with_name("example-historical50.db")
        project_historical_database(self.fixture.harness.config.state_path, self.path, 50)
        with closing(sqlite3.connect(self.path)) as old:
            self.old_columns = columns(old)
            self.before = rows(old, self.old_columns)
            self.old_objects = objects(old)
            self.assertEqual(
                old.execute(
                    "SELECT count(*) FROM codex_turn_controls WHERE send_started_at IS NOT NULL AND interrupt_outcome='unknown'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                old.execute(
                    "SELECT count(*) FROM provider_execution_checkpoints WHERE provider_turn_id='example-turn'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                old.execute(
                    "SELECT count(*) FROM telegram_group_ingress WHERE failure_streak=3 AND failure_threshold_at IS NOT NULL"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                old.execute(
                    "SELECT count(*) FROM codex_telegram_precaution_targets WHERE job_id=? AND ingress_identity='hub'",
                    (self.job_id,),
                ).fetchone()[0],
                1,
            )
            self.assertNotIn("telegram_ingress_watermarks", self.old_columns)
            self.assertNotIn("codex_telegram_ingress_assessments", self.old_columns)

    def test_upgrade_keeps_all_rows_unknown_fence_backup_and_empty_continuity(self) -> None:
        result = migrations.migrate_database(self.path)
        self.assertEqual(
            (result.previous_version, result.current_version),
            (50, migrations.LATEST_SCHEMA_VERSION),
        )
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(rows(backup, self.old_columns), self.before)
            self.assertEqual(objects(backup), self.old_objects)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 50)
        with closing(sqlite3.connect(self.path)) as current:
            self.assertEqual(rows(current, self.old_columns), self.before)
            current_objects = objects(current)
            for value in self.old_objects:
                # Schema52 adds only a nullable column to this parent table.
                # Schema53 preserves the checkpoint DDL before its added notice.
                if value[:2] == ("table", "provider_execution_checkpoints"):
                    actual_sql = next(obj[2] for obj in current_objects if obj[1] == value[1])
                    self.assertTrue(
                        actual_sql.startswith(value[2][:-1] + ", claude_material_notice TEXT")
                    )
                    self.assertEqual(
                        columns(current)[value[1]],
                        self.old_columns[value[1]] + ("claude_material_notice",),
                    )
                elif value[:2] != ("table", "codex_turn_controls"):
                    self.assertIn(value, current_objects)
            for table in ("telegram_ingress_watermarks", "codex_telegram_ingress_assessments"):
                self.assertEqual(current.execute(f"SELECT * FROM {table}").fetchall(), [])
            self.assertEqual(current.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(current.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(migrations.migrate_database(self.path).backup_path)
        with closing(HubState.open(self.path, codex_permission_profile=None)) as state:
            control = state.codex_controls.read(self.job_id)
            assert control is not None
            assessment = state.telegram_ingress_assessments.assess(
                self.job_id, now=datetime.fromisoformat(control["accepted_at"])
            )
            assert assessment.episode is not None
            self.assertEqual(assessment.episode.reason, "poll_failures")
            self.assertEqual(
                state._connection.execute("SELECT * FROM telegram_ingress_watermarks").fetchall(),
                [],
            )
            self.assertEqual(
                {
                    name: [tuple(row) for row in values]
                    for name, values in rows(state._connection, self.old_columns).items()
                },
                self.before,
            )

    def legacy_cause(self, state):
        result = state.telegram_ingress_assessments.assess(self.job_id, now=self.now)
        assert result.episode is not None
        self.assertEqual(result.episode.reason, "poll_failures")
        self.assertEqual(result.episode.since, self.now)
        self.assertEqual(result.episode.deadline, self.now + timedelta(seconds=30))
        row = state.telegram_ingress_assessments.read(self.job_id)
        assert row is not None
        self.assertEqual(row["episode_generation"], 1)
        self.assertEqual((row["recovery_cutoff_epoch"], row["recovery_cutoff_sequence"]), (1, 3))
        self.assertIsNone(row["source_failure_epoch"])
        self.assertIsNone(row["source_failure_sequence"])
        return result.episode, dict(row)

    def assert_cause_preserved(self, state, previous):
        row = state.telegram_ingress_assessments.read(self.job_id)
        assert row is not None
        for name in (
            "episode_generation",
            "reason",
            "since",
            "deadline",
            "recovery_after",
            "recovery_cutoff_epoch",
            "recovery_cutoff_sequence",
            "source_failure_epoch",
            "source_failure_sequence",
        ):
            self.assertEqual(row[name], previous[name], name)

    def test_legacy_equal_deadline_adoption_retains_complete_bundle_then_recovers(self):
        migrations.migrate_database(self.path)
        with closing(HubState.open(self.path, codex_permission_profile=None)) as state:
            episode, before = self.legacy_cause(state)
            self.assertTrue(
                state.telegram_ingress.record_poll(
                    self.owner,
                    sequence=5,
                    succeeded=False,
                    observed_at=self.now + timedelta(seconds=1),
                )
            )
            watermark = state._connection.execute(
                "SELECT * FROM telegram_ingress_watermarks"
            ).fetchone()
            assert watermark is not None
            self.assertEqual(
                (watermark["failure_witness_epoch"], watermark["failure_witness_sequence"]), (1, 5)
            )
            self.assertEqual(watermark["failure_threshold_at"], self.now.isoformat())
            result = state.telegram_ingress_assessments.assess(
                self.job_id, now=self.now + timedelta(seconds=1)
            )
            self.assertEqual(result.episode, episode)
            self.assert_cause_preserved(state, before)
            self.assertTrue(
                state.telegram_ingress.record_poll(
                    self.owner,
                    sequence=6,
                    succeeded=True,
                    observed_at=self.now + timedelta(seconds=2),
                )
            )
            result = state.telegram_ingress_assessments.assess(
                self.job_id, now=self.now + timedelta(seconds=2)
            )
            self.assertTrue(result.recent_poll_confirmed)
            self.assertIsNone(result.episode)
            row = state.telegram_ingress_assessments.read(self.job_id)
            assert row is not None
            self.assertEqual(row["episode_generation"], 1)
            for sequence in (7, 8, 9):
                self.assertTrue(
                    state.telegram_ingress.record_poll(
                        self.owner,
                        sequence=sequence,
                        succeeded=False,
                        observed_at=self.now + timedelta(seconds=3),
                    )
                )
            result = state.telegram_ingress_assessments.assess(
                self.job_id, now=self.now + timedelta(seconds=3)
            )
            assert result.episode is not None
            self.assertEqual(result.episode.deadline, self.now + timedelta(seconds=33))
            row = state.telegram_ingress_assessments.read(self.job_id)
            assert row is not None
            self.assertEqual(row["episode_generation"], 2)
            self.assertEqual(
                (row["recovery_cutoff_sequence"], row["source_failure_sequence"]), (9, 9)
            )

    def test_legacy_assessment_retains_failure_across_reopen_restart_before_capture(self):
        migrations.migrate_database(self.path)
        with closing(HubState.open(self.path, codex_permission_profile=None)) as state:
            episode, before = self.legacy_cause(state)
        with closing(HubState.open(self.path, codex_permission_profile=None)) as state:
            owner = state.telegram_ingress.register(
                "hub",
                instance_token="example-after-upgrade-restart",
                previous_epoch=1,
                now=self.now + timedelta(seconds=1),
            )
            self.assertEqual(
                state._connection.execute("SELECT * FROM telegram_ingress_watermarks").fetchall(),
                [],
            )
            result = state.telegram_ingress_assessments.assess(
                self.job_id, now=self.now + timedelta(seconds=1)
            )
            self.assertFalse(result.recent_poll_confirmed)
            self.assertEqual(result.episode, episode)
            self.assert_cause_preserved(state, before)
            self.assertTrue(
                state.telegram_ingress.record_poll(
                    owner, sequence=1, succeeded=True, observed_at=self.now + timedelta(seconds=2)
                )
            )
            result = state.telegram_ingress_assessments.assess(
                self.job_id, now=self.now + timedelta(seconds=2)
            )
            self.assertTrue(result.recent_poll_confirmed)
            self.assertIsNone(result.episode)
            row = state.telegram_ingress_assessments.read(self.job_id)
            assert row is not None
            self.assertEqual(row["episode_generation"], 1)

    def test_ddl_failure_preserves_exact_old_schema_data_and_user_version(self) -> None:
        with (
            patch.object(
                migrations, "MIGRATION_51", migrations.MIGRATION_51 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(rows(old, self.old_columns), self.before)
            self.assertEqual(objects(old), self.old_objects)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 50)


if __name__ == "__main__":
    unittest.main()
