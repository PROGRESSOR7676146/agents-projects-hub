"""Fictional group poll evidence has no provider, transport or interrupt authority."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from hermes_codex_router.codex_ingress_precaution_policy import assess_ingress
from hermes_codex_router.state import HubState
from hermes_codex_router.state_errors import StateError
from hermes_codex_router.telegram_ingress_ledger import PollSampleRefused, TelegramIngressLedger


class TelegramIngressLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "example-state.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.ledger = TelegramIngressLedger(
            self.state._connection, transaction=self.state._immediate_transaction
        )
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.owner = self.start()

    def start(self, *, identity="hub", token="example-first-instance", previous_epoch=0, now=None):
        return self.ledger.register(
            identity, instance_token=token, previous_epoch=previous_epoch, now=now or self.now
        )

    def poll(self, *, owner=None, sequence=1, success=True, seconds=1):
        return self.ledger.record_poll(
            owner or self.owner,
            sequence=sequence,
            succeeded=success,
            observed_at=self.now + timedelta(seconds=seconds),
        )

    def test_startup_is_unconfirmed_and_empty_successful_poll_establishes_evidence(self):
        initial = self.ledger.read("hub")
        assert initial is not None
        self.assertIsNone(initial.evidence.last_success_at)
        self.assertIsNone(initial.last_confirmed_poll_at)
        self.assertTrue(self.poll())
        current = self.ledger.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.epoch, 1)
        self.assertEqual(current.last_confirmed_poll_at, self.now + timedelta(seconds=1))
        result = assess_ingress(
            current.evidence,
            expected_identity="hub",
            accepted_at=self.now,
            now=self.now + timedelta(seconds=1),
            last_confirmed_poll_at=current.last_confirmed_poll_at,
        )
        self.assertTrue(result.recent_poll_confirmed)

    def test_restart_preserves_history_but_does_not_claim_current_success(self):
        self.poll(seconds=3600)
        second = self.start(
            token="example-second-instance",
            previous_epoch=1,
            now=self.now + timedelta(seconds=3601),
        )
        current = self.ledger.read("hub")
        assert current is not None
        self.assertEqual(second.epoch, 2)
        self.assertIsNone(current.evidence.last_success_at)
        self.assertEqual(current.last_confirmed_poll_at, self.now + timedelta(seconds=3600))
        pending = assess_ingress(
            current.evidence,
            expected_identity="hub",
            accepted_at=self.now,
            now=self.now + timedelta(seconds=3601),
            last_confirmed_poll_at=current.last_confirmed_poll_at,
        )
        assert pending.episode is not None
        self.assertEqual(pending.episode.deadline, self.now + timedelta(seconds=3780))
        self.poll(owner=second, seconds=3602)
        recovered = self.ledger.read("hub")
        assert recovered is not None
        self.assertTrue(
            assess_ingress(
                recovered.evidence,
                expected_identity="hub",
                accepted_at=self.now,
                now=self.now + timedelta(seconds=3602),
                prior=pending.episode,
                last_confirmed_poll_at=pending.last_confirmed_poll_at,
            ).recent_poll_confirmed
        )

    def test_registration_repeat_is_idempotent_and_stale_owner_cannot_reclaim(self):
        self.poll()
        repeated = self.start(now=self.now + timedelta(seconds=3))
        self.assertEqual(repeated, self.owner)
        before = self.ledger.read("hub")
        second = self.start(
            token="example-second-instance", previous_epoch=1, now=self.now + timedelta(seconds=4)
        )
        with self.assertRaises(StateError):
            self.start(now=self.now + timedelta(seconds=5))
        self.assertFalse(self.poll(seconds=6))
        self.assertFalse(
            self.poll(owner=replace(second, instance_token=self.owner.instance_token), seconds=6)
        )
        assert before is not None
        self.assertEqual(before.evidence.last_success_at, self.now + timedelta(seconds=1))
        retained = self.ledger.read("hub")
        assert retained is not None
        self.assertEqual(retained.evidence.epoch, 2)

    def test_exact_repeat_after_uncertain_commit_does_not_count_failure_twice(self):
        self.assertTrue(self.poll(success=False))
        self.assertTrue(self.poll(success=False))
        with self.assertRaises(PollSampleRefused):
            self.poll(success=True)
        with self.assertRaises(PollSampleRefused):
            self.poll(success=False, seconds=2)
        current = self.ledger.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 1)
        self.assertIsNone(current.evidence.failure_threshold_at)

    def test_third_failure_threshold_is_immutable_until_actual_success(self):
        for sequence in range(1, 8):
            self.assertTrue(self.poll(sequence=sequence, success=False, seconds=sequence))
        current = self.ledger.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 7)
        self.assertEqual(current.evidence.failure_threshold_at, self.now + timedelta(seconds=3))
        self.poll(sequence=8, seconds=8)
        restored = self.ledger.read("hub")
        assert restored is not None
        self.assertEqual(restored.evidence.failure_streak, 0)
        self.assertIsNone(restored.evidence.failure_threshold_at)

    def test_sequence_gap_breaks_unproven_consecutive_failures(self):
        self.poll(success=False)
        self.poll(sequence=2, success=False, seconds=2)
        self.poll(sequence=4, success=False, seconds=4)
        current = self.ledger.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 1)
        self.assertIsNone(current.evidence.failure_threshold_at)
        with self.assertRaises(PollSampleRefused):
            self.poll(sequence=3, success=True, seconds=3)

    def test_wrong_identity_cannot_establish_other_ingress_success(self):
        codex = self.start(identity="codex", token="example-codex-instance")
        self.poll(owner=codex)
        current = self.ledger.read("hub")
        assert current is not None
        self.assertIsNone(current.evidence.last_success_at)

    def test_competing_connections_fence_stale_registration_and_poll(self):
        other = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(other.close)
        ledger = TelegramIngressLedger(other._connection, transaction=other._immediate_transaction)
        second = ledger.register(
            "hub",
            instance_token="example-second-instance",
            previous_epoch=1,
            now=self.now + timedelta(seconds=2),
        )
        with self.assertRaises(StateError):
            self.start(
                token="example-competing-instance",
                previous_epoch=1,
                now=self.now + timedelta(seconds=3),
            )
        self.assertFalse(self.poll(seconds=3))
        self.assertTrue(
            ledger.record_poll(
                second, sequence=1, succeeded=True, observed_at=self.now + timedelta(seconds=3)
            )
        )

    def test_commit_fault_rolls_back_sample_and_retry_remains_safe(self):
        def deny_commit(action, first, second, database, trigger):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
                else sqlite3.SQLITE_OK
            )

        self.state._connection.set_authorizer(deny_commit)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.poll(success=False)
        finally:
            self.state._connection.set_authorizer(None)
        current = self.ledger.read("hub")
        assert current is not None
        self.assertIsNone(current.evidence.last_poll_at)
        self.assertTrue(self.poll(success=False))
        retry = self.ledger.read("hub")
        assert retry is not None
        self.assertEqual(retry.evidence.failure_streak, 1)

    def test_invalid_shapes_and_backward_clock_cannot_create_new_evidence(self):
        self.poll(seconds=5)
        for sequence in (True, 0, -1, 2**63):
            with self.subTest(sequence=sequence), self.assertRaises(StateError):
                self.poll(sequence=sequence)
        with self.assertRaises(StateError):
            self.ledger.record_poll(
                self.owner, sequence=2, succeeded=cast(bool, 1), observed_at=self.now
            )
        with self.assertRaises(StateError):
            self.ledger.record_poll(
                self.owner, sequence=2, succeeded=True, observed_at=self.now.replace(tzinfo=None)
            )
        with self.assertRaises(PollSampleRefused):
            self.poll(sequence=2, seconds=4)
        with self.assertRaises(StateError):
            self.start(identity="claude", token="example-other")
        with self.assertRaises(StateError):
            self.start(token="example-next", previous_epoch=True)


if __name__ == "__main__":
    unittest.main()
