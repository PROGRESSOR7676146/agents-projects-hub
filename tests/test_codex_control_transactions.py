"""Shared state transactions retain assessments and existing control fences together.

Protective control here has independent existing authority; an ingress episode
does not grant it. No native client or Telegram transport is used.
"""

from __future__ import annotations

import sqlite3
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

from hermes_codex_router.codex_turn_controls import ActiveTurnProof
from hermes_codex_router.state import HubState, StateError
from tests import test_telegram_turn_provenance as fixtures


class CodexControlTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramTurnProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.state = self.fixture.state
        self.job, _ = self.fixture.enqueue("hub")
        _, self.token = self.fixture.accept(self.job)
        self.controls = self.state.codex_controls
        self.assessments = self.state.telegram_ingress_assessments
        self.now = datetime.fromisoformat(self.control()["accepted_at"])
        self.root = str(self.fixture.harness.root)
        self.owner = self.state.telegram_ingress.register(
            "hub", instance_token="example-transaction-publisher", previous_epoch=0, now=self.now
        )

    def control(self) -> sqlite3.Row:
        row = self.controls.read(self.job.job_id)
        assert row is not None
        return row

    def assessment(self) -> dict | None:
        row = self.assessments.read(self.job.job_id)
        return None if row is None else dict(row)

    def begin(self, token: str | None = None) -> str | None:
        return self.controls.begin_interrupt_in_transaction(
            job_id=self.job.job_id,
            source="protective",
            proof=ActiveTurnProof("example-thread", "example-turn", self.root, time.monotonic()),
            validated_root=self.root,
            invocation_token=self.token if token is None else token,
            now=self.now,
        )

    def assess(self):
        return self.assessments.assess_in_transaction(self.job.job_id, now=self.now)

    def retained_before_failure(self):
        self.assessments.assess(self.job.job_id, now=self.now)
        for sequence in (1, 2, 3):
            self.state.telegram_ingress.record_poll(
                self.owner, sequence=sequence, succeeded=False, observed_at=self.now
            )
        return self.assessment(), dict(self.control())

    def assert_rolled_back(self, before) -> None:
        self.assertFalse(self.state._connection.in_transaction)
        self.assertEqual((self.assessment(), dict(self.control())), before)

    def test_primitives_without_transaction_refuse_without_writes(self) -> None:
        before = self.assessment(), dict(self.control())
        with self.assertRaisesRegex(StateError, "transaction"):
            self.assess()
        with self.assertRaisesRegex(StateError, "transaction"):
            self.begin()
        self.assert_rolled_back(before)

    def test_one_outer_commit_publishes_both_effects_and_retains_exclusion(self) -> None:
        with closing(
            HubState.open_existing(
                self.fixture.harness.config.state_path, codex_permission_profile=None
            )
        ) as peer:
            with self.state._immediate_transaction():
                self.assess()
                owner = self.begin()
                self.assertIsNotNone(owner)
                self.assertTrue(self.state._connection.in_transaction)
                self.assertIsNone(peer.telegram_ingress_assessments.read(self.job.job_id))
                saved = peer.codex_controls.read(self.job.job_id)
                assert saved is not None
                self.assertIsNone(saved["send_started_at"])
            self.assertIsNotNone(peer.telegram_ingress_assessments.read(self.job.job_id))
            saved = peer.codex_controls.read(self.job.job_id)
            assert saved is not None
            self.assertIsNotNone(saved["send_started_at"])
            self.assertIsNone(saved["owner_quiesced_at"])
            with self.state._immediate_transaction():
                self.assertIsNone(self.begin())
            self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "executing")

    def test_exception_after_assessment_rolls_back_cause_and_revision(self) -> None:
        before = self.retained_before_failure()
        with self.assertRaisesRegex(RuntimeError, "example interrupted composition"):
            with self.state._immediate_transaction():
                self.assess()
                self.assertNotEqual(self.assessment(), before[0])
                raise RuntimeError("example interrupted composition")
        self.assert_rolled_back(before)

    def test_exception_after_send_reservation_rolls_back_both_effects(self) -> None:
        before = self.retained_before_failure()
        with self.assertRaisesRegex(RuntimeError, "example interrupted composition"):
            with self.state._immediate_transaction():
                self.assess()
                self.assertIsNotNone(self.begin())
                raise RuntimeError("example interrupted composition")
        self.assert_rolled_back(before)

    def test_fence_write_failure_rolls_back_assessment(self) -> None:
        before = self.retained_before_failure()
        self.state._connection.execute(
            """CREATE TEMP TRIGGER example_refuse_fence BEFORE UPDATE ON codex_turn_controls
               WHEN NEW.send_started_at IS NOT NULL
               BEGIN SELECT RAISE(ABORT, 'example fence write fault'); END"""
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "example fence write fault"):
            with self.state._immediate_transaction():
                self.assess()
                self.begin()
        self.assert_rolled_back(before)

    def test_commit_failure_rolls_back_cause_revision_and_sender_owner(self) -> None:
        before = self.retained_before_failure()

        def deny_commit(action, argument, _second, _database, _trigger):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT"
                else sqlite3.SQLITE_OK
            )

        self.state._connection.set_authorizer(deny_commit)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                with self.state._immediate_transaction():
                    self.assess()
                    self.assertIsNotNone(self.begin())
        finally:
            self.state._connection.set_authorizer(None)
        self.assert_rolled_back(before)

    def test_guard_refusal_can_commit_assessment_without_send_owner(self) -> None:
        with self.state._immediate_transaction():
            self.assess()
            self.assertIsNone(self.begin("example-wrong-lease"))
        self.assertIsNotNone(self.assessment())
        self.assertIsNone(self.control()["send_started_at"])

    def test_incoherent_assessment_is_not_health_or_interrupt_authority(self) -> None:
        before = self.retained_before_failure()
        with self.assertRaisesRegex(StateError, "timezone-aware"):
            with self.state._immediate_transaction():
                self.assessments.assess_in_transaction(self.job.job_id, now=datetime(2026, 1, 1))
                self.begin()
        self.assert_rolled_back(before)

    def test_nested_wrappers_refuse_without_ending_callers_transaction(self) -> None:
        with self.state._immediate_transaction():
            self.assess()
            with self.assertRaisesRegex(StateError, "cannot nest"):
                self.assessments.assess(self.job.job_id, now=self.now)
            with self.assertRaisesRegex(StateError, "cannot nest"):
                self.controls.begin_interrupt(
                    job_id=self.job.job_id,
                    source="protective",
                    proof=ActiveTurnProof(
                        "example-thread", "example-turn", self.root, time.monotonic()
                    ),
                    validated_root=self.root,
                    invocation_token=self.token,
                    now=self.now,
                )
            self.assertTrue(self.state._connection.in_transaction)
            self.assertIsNotNone(self.begin())
        self.assertIsNotNone(self.assessment())
        self.assertIsNotNone(self.control()["send_started_at"])

    def assert_stop_schedule(self, seconds: int | None) -> None:
        scheduled = (
            None
            if seconds is None
            else (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        )
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """UPDATE codex_turn_controls SET next_late_read_at=?,late_read_attempts=2,
                   read_claim_token='example-read-claim',read_claim_owner='example-maintainer',
                   read_claim_expires_at=? WHERE job_id=?""",
                (scheduled, (self.now + timedelta(seconds=300)).isoformat(), self.job.job_id),
            )
        before = dict(self.control())
        request_id, _, _ = self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        stop = self.state._connection.execute(
            "SELECT created_at FROM provider_stop_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        assert stop is not None
        after = dict(self.control())
        self.assertEqual(after["stop_request_id"], request_id)
        self.assertEqual(
            after["next_late_read_at"],
            max(value for value in (scheduled, stop["created_at"]) if value is not None),
        )
        for key in (
            "late_read_attempts",
            "read_claim_token",
            "read_claim_owner",
            "read_claim_expires_at",
        ):
            self.assertEqual(after[key], before[key])
        self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=100,
            target_agent_id="codex",
        )
        self.assertEqual(dict(self.control()), after)

    def test_stop_initializes_null_schedule_without_resetting_claim_or_attempts(self) -> None:
        self.assert_stop_schedule(None)

    def test_stop_advances_earlier_schedule_without_resetting_claim_or_attempts(self) -> None:
        self.assert_stop_schedule(-30)

    def test_stop_preserves_later_schedule_without_resetting_claim_or_attempts(self) -> None:
        self.assert_stop_schedule(180)


if __name__ == "__main__":
    unittest.main()
