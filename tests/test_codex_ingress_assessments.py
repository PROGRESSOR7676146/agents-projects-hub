"""Persisted causal poll evidence never grants native control authority."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta

from hermes_codex_router.state import HubState
from hermes_codex_router.state_errors import StateError
from tests import test_telegram_turn_provenance as fixtures


class CodexIngressAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TelegramTurnProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.state = self.fixture.state
        self.job, _ = self.fixture.enqueue("hub")
        self.fixture.accept(self.job)
        control = self.state.codex_controls.read(self.job.job_id)
        assert control is not None
        self.now = datetime.fromisoformat(control["accepted_at"])
        self.owner = self.state.telegram_ingress.register(
            "hub", instance_token="example-first-publisher", previous_epoch=0, now=self.now
        )

    def poll(self, sequence, success=False, seconds=0, owner=None):
        return self.state.telegram_ingress.record_poll(
            owner or self.owner,
            sequence=sequence,
            succeeded=success,
            observed_at=self.now + timedelta(seconds=seconds),
        )

    def assess(self, seconds=0):
        return self.state.telegram_ingress_assessments.assess(
            self.job.job_id, now=self.now + timedelta(seconds=seconds)
        )

    def row(self):
        row = self.state.telegram_ingress_assessments.read(self.job.job_id)
        assert row is not None
        return dict(row)

    def restart(self, seconds=0):
        self.owner = self.state.telegram_ingress.register(
            "hub",
            instance_token="example-second-publisher",
            previous_epoch=1,
            now=self.now + timedelta(seconds=seconds),
        )

    def test_established_failure_survives_gap_restart_and_equal_time_old_success(self):
        self.poll(1, True)
        for sequence in (2, 3, 4):
            self.poll(sequence)
        self.poll(6)
        assessment = self.assess()
        self.assertFalse(assessment.recent_poll_confirmed)
        assert assessment.episode is not None
        self.assertEqual(assessment.episode.reason, "poll_failures")
        self.assertEqual(assessment.episode.deadline, self.now + timedelta(seconds=30))
        self.restart()
        self.assertEqual(self.assess(31).episode, assessment.episode)
        self.assertEqual(self.row()["episode_generation"], 1)

    def test_new_failure_after_unobserved_recovery_creates_new_generation_deadline(self):
        for sequence in (1, 2, 3):
            self.poll(sequence)
        old = self.assess()
        self.poll(4, True, seconds=10)
        for sequence in (5, 6, 7):
            self.poll(sequence, seconds=20)
        new = self.assess(20)
        assert old.episode is not None and new.episode is not None
        self.assertEqual(old.episode.deadline, self.now + timedelta(seconds=30))
        self.assertEqual(new.episode.deadline, self.now + timedelta(seconds=50))
        self.assertEqual(self.row()["episode_generation"], 2)

    def test_recovery_before_restart_retires_old_cause_without_current_health(self):
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.assess()
        self.poll(4, True)
        self.restart()
        result = self.assess()
        self.assertFalse(result.recent_poll_confirmed)
        assert result.episode is not None
        self.assertEqual(result.episode.reason, "stale_or_missing")
        self.assertEqual(self.row()["episode_generation"], 2)
        self.assertEqual(self.row()["recovery_cutoff_epoch"], 2)
        self.assertEqual(self.row()["recovery_cutoff_sequence"], 0)
        self.assertEqual(self.assess().episode, result.episode)
        self.poll(1, True)
        self.assertTrue(self.assess().recent_poll_confirmed)

    def test_same_timestamp_success_recovers_only_logically_earlier_cause(self):
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.assess()
        self.poll(4, True)
        for sequence in (5, 6, 7):
            self.poll(sequence)
        result = self.assess()
        self.assertFalse(result.recent_poll_confirmed)
        self.assertIsNotNone(result.episode)
        self.assertEqual(self.row()["episode_generation"], 2)
        self.poll(8, True)
        self.assertTrue(self.assess().recent_poll_confirmed)
        self.assertIsNone(self.row()["reason"])
        self.assertEqual(self.row()["episode_generation"], 2)

    def test_logically_later_success_before_recovery_clock_does_not_clear(self):
        first = self.assess(100)
        self.poll(1, True, seconds=90)
        result = self.assess(100)
        self.assertFalse(result.recent_poll_confirmed)
        self.assertEqual(result.episode, first.episode)
        self.assertEqual(self.row()["episode_generation"], 1)

    def test_unknown_target_and_assessment_clock_regression_do_not_create_evidence(self):
        other = fixtures.TelegramTurnProvenanceTests()
        other.setUp()
        self.addCleanup(lambda: self.assertTrue(other.doCleanups()))
        unknown, _ = other.enqueue()
        other.accept(unknown)
        self.assertIsNotNone(other.state.codex_controls.read(unknown.job_id))
        self.assertIsNone(other.state.telegram_turn_provenance.target(unknown.job_id))
        with self.assertRaises(StateError):
            other.state.telegram_ingress_assessments.assess(unknown.job_id, now=self.now)
        self.assess(10)
        before = self.row()
        with self.assertRaises(StateError):
            self.assess(9)
        self.assertEqual(self.row(), before)

    def test_selected_cause_keeps_all_metadata_until_strictly_earlier_candidate(self):
        initial = self.assess()
        before = self.row()
        for sequence in (1, 2, 3):
            self.poll(sequence, seconds=100)
        later = self.assess(100)
        self.assertEqual(later.episode, initial.episode)
        for name in (
            "reason",
            "since",
            "deadline",
            "recovery_after",
            "recovery_cutoff_epoch",
            "recovery_cutoff_sequence",
            "source_failure_epoch",
            "source_failure_sequence",
        ):
            self.assertEqual(self.row()[name], before[name])
        self.assertEqual(self.row()["episode_generation"], 1)

    def test_earlier_failure_reclassifies_complete_bundle_in_same_generation(self):
        self.assess()
        for sequence in (1, 2, 3):
            self.poll(sequence, seconds=10)
        result = self.assess(10)
        assert result.episode is not None
        self.assertEqual(result.episode.reason, "poll_failures")
        self.assertEqual(result.episode.deadline, self.now + timedelta(seconds=40))
        row = self.row()
        self.assertEqual(
            (
                row["episode_generation"],
                row["recovery_cutoff_epoch"],
                row["recovery_cutoff_sequence"],
                row["source_failure_sequence"],
            ),
            (1, 1, 3, 3),
        )
        self.assertEqual(row["recovery_after"], (self.now + timedelta(seconds=10)).isoformat())

    def test_watermark_failure_cutoff_remains_actual_witness_after_gap(self):
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.poll(7)
        self.assess()
        row = self.row()
        self.assertEqual(
            (
                row["last_read_sequence"],
                row["recovery_cutoff_sequence"],
                row["source_failure_sequence"],
            ),
            (7, 3, 3),
        )

    def test_healthy_confirmation_survives_database_reopen_and_new_epoch(self):
        self.poll(1, True, seconds=1000)
        self.assertTrue(self.assess(1000).recent_poll_confirmed)
        path = self.fixture.harness.config.state_path
        self.state.close()
        self.state = HubState.open(path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.restart(seconds=1001)
        result = self.assess(1001)
        self.assertFalse(result.recent_poll_confirmed)
        assert result.episode is not None
        self.assertEqual(result.episode.deadline, self.now + timedelta(seconds=1180))
        self.assertEqual(self.row()["episode_generation"], 1)

    def test_absent_baseline_is_distinct_from_registration_and_wrong_ingress(self):
        other = fixtures.TelegramTurnProvenanceTests()
        other.setUp()
        self.addCleanup(lambda: self.assertTrue(other.doCleanups()))
        job, _ = other.enqueue("hub")
        other.accept(job)
        other.state.telegram_ingress_assessments.assess(job.job_id, now=self.now)
        absent = other.state.telegram_ingress_assessments.read(job.job_id)
        assert absent is not None
        self.assertIsNone(absent["last_read_epoch"])
        self.assertIsNone(absent["recovery_cutoff_epoch"])
        self.assess()
        registered = self.row()
        self.assertEqual(
            (registered["recovery_cutoff_epoch"], registered["recovery_cutoff_sequence"]), (1, 0)
        )
        codex = self.state.telegram_ingress.register(
            "codex", instance_token="example-other-ingress", previous_epoch=0, now=self.now
        )
        self.poll(1, True, owner=codex)
        self.assertIsNotNone(self.assess().episode)

    def test_assessment_commit_failure_during_recovery_is_atomic_and_retry_converges(self):
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.assess()
        before = self.row()
        self.poll(4, True)

        def deny(action, first, second, database, trigger):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
                else sqlite3.SQLITE_OK
            )

        self.state._connection.set_authorizer(deny)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.assess()
        finally:
            self.state._connection.set_authorizer(None)
        self.assertEqual(self.row(), before)
        with closing(
            HubState.open(self.fixture.harness.config.state_path, codex_permission_profile=None)
        ) as other:
            row = other.telegram_ingress_assessments.read(self.job.job_id)
            assert row is not None
            self.assertEqual(dict(row), before)
        self.assertTrue(self.assess().recent_poll_confirmed)
        self.assertEqual(self.row()["episode_generation"], 1)

    def test_malformed_or_future_watermark_refuses_without_assessment_mutation(self):
        self.poll(1, True)
        self.assess()
        before = self.row()
        trigger = self.state._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='telegram_ingress_watermark_fence'"
        ).fetchone()
        assert trigger is not None
        for updates in (
            "success_epoch=2",
            "success_at='2026-01-01T00:00:00'",
            "success_at='2999-01-01T00:00:00+00:00'",
        ):
            with self.subTest(updates=updates):
                original = self.state._connection.execute(
                    "SELECT * FROM telegram_ingress_watermarks"
                ).fetchone()
                assert original is not None
                self.state._connection.execute("BEGIN IMMEDIATE")
                try:
                    self.state._connection.execute("DROP TRIGGER telegram_ingress_watermark_fence")
                    self.state._connection.execute(
                        f"UPDATE telegram_ingress_watermarks SET {updates}"
                    )
                    # Reads participate in one owner transaction; corruption is
                    # committed only within this disposable fictional fixture.
                    self.state._connection.commit()
                    with self.assertRaises(StateError):
                        self.assess()
                    self.assertEqual(self.row(), before)
                finally:
                    with self.state._immediate_transaction():
                        self.state._connection.execute(
                            "UPDATE telegram_ingress_watermarks SET success_epoch=?,success_at=?",
                            (original["success_epoch"], original["success_at"]),
                        )
                        self.state._connection.execute(trigger[0])

    def test_assessment_sql_preserves_generations_bundles_and_rows(self):
        self.assess()
        before = self.row()
        self.state._connection.execute("PRAGMA recursive_triggers=OFF")
        for update in (
            "rowid=100",
            "assessment_revision=assessment_revision",
            "assessment_revision=assessment_revision+1,deadline='2999-01-01T00:00:00+00:00'",
            "assessment_revision=assessment_revision+1,recovery_cutoff_sequence=1",
            "assessment_revision=assessment_revision+1,reason=NULL,since=NULL,deadline=NULL,recovery_after=NULL,recovery_cutoff_epoch=NULL,recovery_cutoff_sequence=NULL,episode_generation=episode_generation+1",
        ):
            with (
                self.subTest(update=update),
                self.assertRaises(sqlite3.IntegrityError),
                self.state._immediate_transaction(),
            ):
                self.state._connection.execute(
                    f"UPDATE OR REPLACE codex_telegram_ingress_assessments SET {update}"
                )
            self.assertEqual(self.row(), before)
        names = ",".join(before)
        values = ",".join("?" for _ in before)
        with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
            self.state._connection.execute(
                f"INSERT OR REPLACE INTO codex_telegram_ingress_assessments ({names}) VALUES ({values})",
                tuple(before.values()),
            )
        self.assertEqual(self.row(), before)
        self.poll(1, True)
        self.assess()
        healthy = self.row()
        with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE codex_telegram_ingress_assessments SET assessment_revision=assessment_revision+1,reason='never_confirmed',since=?,deadline=?,recovery_after=?,recent_poll_confirmed=0",
                (
                    self.now.isoformat(),
                    (self.now + timedelta(seconds=120)).isoformat(),
                    self.now.isoformat(),
                ),
            )
        self.assertEqual(self.row(), healthy)

    def test_polling_and_assessment_leave_all_native_and_delivery_state_unchanged(self):
        def untouched():
            return {
                row[0]: tuple(
                    tuple(value)
                    for value in self.state._connection.execute(f'SELECT * FROM "{row[0]}"')
                )
                for row in self.state._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                if row[0]
                not in {
                    "telegram_group_ingress",
                    "telegram_ingress_watermarks",
                    "codex_telegram_ingress_assessments",
                }
            }

        before = untouched()
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.assess(100)
        self.poll(4, True, seconds=100)
        self.assess(100)
        self.assertEqual(untouched(), before)

    def test_fractional_retained_ledger_fields_refuse_without_assessment_mutation(self):
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.assess()
        before = self.row()
        trigger = self.state._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='telegram_group_ingress_fence'"
        ).fetchone()
        assert trigger is not None
        for column, value in (("failure_streak", 3.5), ("poll_sequence", 3.5), ("epoch", 1.5)):
            with self.subTest(column=column):
                with self.state._immediate_transaction():
                    self.state._connection.execute("DROP TRIGGER telegram_group_ingress_fence")
                    self.state._connection.execute(
                        f"UPDATE telegram_group_ingress SET {column}=?", (value,)
                    )
                with self.assertRaises(StateError):
                    self.assess()
                self.assertEqual(self.row(), before)
                with self.state._immediate_transaction():
                    self.state._connection.execute(
                        f"UPDATE telegram_group_ingress SET {column}=?",
                        (1 if column == "epoch" else 3,),
                    )
                    self.state._connection.execute(trigger[0])


if __name__ == "__main__":
    unittest.main()
