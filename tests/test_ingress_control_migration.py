"""Populated released schema51 upgrades without inventing send causes."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import HubState
from tests import test_codex_ingress_control as fixtures
from tests.schema_fixtures import project_historical_database
from tests.test_codex_control_migration import columns, rows
from tests.test_delivery_control_activation_migration import objects


class IngressControlMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture = fixtures.CodexIngressControlTests()
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups()))
        state = fixture.state
        owner = state.codex_controls.begin_interrupt(
            job_id=fixture.job.job_id,
            source="protective",
            proof=fixture.proof(),
            validated_root=fixture.root,
            invocation_token=fixture.token,
            now=fixture.now,
        )
        assert owner is not None
        state.codex_controls.finish_interrupt(
            fixture.job.job_id, owner, outcome="unknown", send_path_quiesced=False, now=fixture.now
        )
        fixture.indeterminate()
        assert fixture.claim() is not None
        self.path = fixture.fixture.harness.config.state_path.with_name("example-historical51.db")
        project_historical_database(fixture.fixture.harness.config.state_path, self.path, 51)
        with closing(sqlite3.connect(self.path)) as old:
            self.old_columns = columns(old)
            self.before = rows(old, self.old_columns)
            self.old_objects = objects(old)
            self.assertNotIn("codex_ingress_interrupt_causes", self.old_columns)
            self.assertNotIn(
                "ingress_assessment_revision_at_send", self.old_columns["codex_turn_controls"]
            )
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 51)
            self.assertEqual(
                old.execute(
                    "SELECT late_read_attempts,interrupt_outcome FROM codex_turn_controls"
                ).fetchone(),
                (1, "unknown"),
            )
            self.assertEqual(
                old.execute(
                    "SELECT count(*) FROM codex_telegram_ingress_assessments WHERE reason='poll_failures'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                old.execute(
                    "SELECT count(*) FROM telegram_ingress_watermarks WHERE failure_witness_sequence=3"
                ).fetchone()[0],
                1,
            )

    def test_upgrade_preserves_old_rows_objects_claim_unknown_owner_and_backup(self):
        result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (51, 52))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(rows(backup, self.old_columns), self.before)
            self.assertEqual(objects(backup), self.old_objects)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 51)
        with closing(sqlite3.connect(self.path)) as current:
            self.assertEqual(rows(current, self.old_columns), self.before)
            current_objects = objects(current)
            for definition in self.old_objects:
                # Only the parent CREATE TABLE gains the nullable discriminator.
                if definition[1] == "codex_turn_controls":
                    anchor = "owner_quiesced_at TEXT,"
                    self.assertEqual(definition[2].count(anchor), 1)
                    expected_sql = definition[2].replace(
                        anchor,
                        "owner_quiesced_at TEXT, ingress_assessment_revision_at_send INTEGER\n"
                        "    CHECK(ingress_assessment_revision_at_send IS NULL OR\n"
                        "      (typeof(ingress_assessment_revision_at_send)='integer'\n"
                        "       AND ingress_assessment_revision_at_send BETWEEN 1 AND 9223372036854775807)),",
                    )
                    actual_sql = next(obj[2] for obj in current_objects if obj[1] == definition[1])
                    self.assertEqual(actual_sql, expected_sql)
                else:
                    self.assertIn(definition, current_objects)
            self.assertEqual(
                current.execute("SELECT * FROM codex_ingress_interrupt_causes").fetchall(), []
            )
            self.assertEqual(
                current.execute(
                    "SELECT ingress_assessment_revision_at_send FROM codex_turn_controls"
                ).fetchall(),
                [(None,)],
            )
            self.assertEqual(current.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(current.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_old_send_cannot_gain_cause_after_upgrade(self):
        migrations.migrate_database(self.path)
        with closing(HubState.open_existing(self.path, codex_permission_profile=None)) as state:
            row = state.codex_controls.read(self.fixture.job.job_id)
            assert row is not None
            before = dict(row)
            self.assertIsNone(
                state.codex_ingress_control.begin_interrupt(
                    job_id=self.fixture.job.job_id,
                    proof=self.fixture.proof(),
                    validated_root=self.fixture.root,
                    read_claim_token=row["read_claim_token"],
                    now=self.fixture.now,
                )
            )
            row = state.codex_controls.read(self.fixture.job.job_id)
            assert row is not None
            self.assertEqual(dict(row), before)
            self.assertIsNone(state.codex_ingress_control.read_cause(self.fixture.job.job_id))

    def test_unsent_genuine_schema51_target_retains_first_send_authority_after_upgrade(self):
        fixture = fixtures.CodexIngressControlTests()
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups()))
        path = self.path.with_name("example-unsent51.db")
        project_historical_database(fixture.fixture.harness.config.state_path, path, 51)
        with closing(sqlite3.connect(path)) as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 51)
            self.assertNotIn(
                "ingress_assessment_revision_at_send", columns(old)["codex_turn_controls"]
            )
            self.assertIsNone(
                old.execute("SELECT send_started_at FROM codex_turn_controls").fetchone()[0]
            )
        migrations.migrate_database(path)
        with closing(HubState.open_existing(path, codex_permission_profile=None)) as state:
            owner = state.codex_ingress_control.begin_interrupt(
                job_id=fixture.job.job_id,
                proof=fixture.proof(),
                validated_root=fixture.root,
                invocation_token=fixture.token,
                now=fixture.now,
            )
            assert owner is not None
            cause = state.codex_ingress_control.read_cause(fixture.job.job_id)
            control = state.codex_controls.read(fixture.job.job_id)
            assert cause is not None and control is not None
            self.assertEqual(
                cause["assessment_revision"], control["ingress_assessment_revision_at_send"]
            )
            self.assertEqual(cause["reason"], "poll_failures")
            self.assertIsNotNone(control["send_started_at"])
            self.assertIsNone(control["owner_quiesced_at"])

    def test_ddl_fault_rolls_back_add_column_causes_objects_data_and_version(self):
        with (
            patch.object(
                migrations, "MIGRATION_52", migrations.MIGRATION_52 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(rows(old, self.old_columns), self.before)
            self.assertEqual(objects(old), self.old_objects)
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 51)

    def test_genuine_precontrol_checkpoint_stays_legacy_without_ingress_authority(self):
        path = self.path.with_name("example-historical47.db")
        project_historical_database(self.fixture.fixture.harness.config.state_path, path, 47)
        migrations.migrate_database(path)
        with closing(HubState.open_existing(path, codex_permission_profile=None)) as state:
            control = state.codex_controls.read(self.fixture.job.job_id)
            assert control is not None
            self.assertEqual(control["origin"], "legacy_read_only")
            self.assertIsNone(
                state.codex_ingress_control.claim_read(
                    self.fixture.job.job_id, "example-reader", now=self.fixture.now
                )
            )
            self.assertIsNone(
                state.codex_ingress_control.begin_interrupt(
                    job_id=self.fixture.job.job_id,
                    proof=self.fixture.proof(),
                    validated_root=self.fixture.root,
                    invocation_token=self.fixture.token,
                    now=self.fixture.now,
                )
            )
            self.assertIsNone(state.codex_ingress_control.read_cause(self.fixture.job.job_id))


if __name__ == "__main__":
    unittest.main()
