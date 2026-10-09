"""Dormant ingress authority shares exact fences and observation budgets.

Only fictional SQLite state and active proofs are used; there is no runtime
scheduler, Telegram transport or native client in this slice.
"""

from __future__ import annotations

import sqlite3
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from hermes_codex_router.codex_turn_controls import ActiveTurnProof
from hermes_codex_router.state import HubState, StateError
from tests import test_telegram_turn_provenance as fixtures


class CodexIngressControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramTurnProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.state = self.fixture.state
        self.job, _ = self.fixture.enqueue("hub")
        _, self.token = self.fixture.accept(self.job)
        self.root = str(self.fixture.harness.root)
        self.accepted_at = datetime.fromisoformat(self.control()["accepted_at"])
        self.now = self.accepted_at + timedelta(seconds=31)
        self.publisher = self.state.telegram_ingress.register(
            "hub", instance_token="example-ingress-control", previous_epoch=0, now=self.accepted_at
        )
        for sequence in (1, 2, 3):
            self.poll(sequence, succeeded=False)

    def control(self) -> sqlite3.Row:
        row = self.state.codex_controls.read(self.job.job_id)
        assert row is not None
        return row

    def poll(self, sequence: int, *, succeeded: bool, at: datetime | None = None) -> None:
        self.state.telegram_ingress.record_poll(
            self.publisher,
            sequence=sequence,
            succeeded=succeeded,
            observed_at=self.accepted_at if at is None else at,
        )

    def proof(self) -> ActiveTurnProof:
        return ActiveTurnProof("example-thread", "example-turn", self.root, time.monotonic())

    def begin(self, *, proof: ActiveTurnProof | None = None, token: str | None = None):
        return self.state.codex_ingress_control.begin_interrupt(
            job_id=self.job.job_id,
            proof=self.proof() if proof is None else proof,
            validated_root=self.root,
            invocation_token=self.token if token is None else token,
            now=self.now,
        )

    def cause(self) -> sqlite3.Row | None:
        return self.state.codex_ingress_control.read_cause(self.job.job_id)

    def indeterminate(self) -> None:
        self.state.mark_provider_job_indeterminate(
            self.job.job_id,
            self.token,
            error_code="example-native-loss",
            error_detail="Fictional unconfirmed turn",
        )

    def claim(self, *, now: datetime | None = None):
        return self.state.codex_ingress_control.claim_read(
            self.job.job_id, "example-maintainer", now=self.now if now is None else now
        )

    def test_due_invocation_reserves_fence_and_immutable_cause_without_stop(self):
        owner = self.begin()
        self.assertIsNotNone(owner)
        cause = self.cause()
        assert cause is not None
        assessment = self.state.telegram_ingress_assessments.read(self.job.job_id)
        assert assessment is not None
        self.assertEqual(self.control()["interrupt_source"], "protective")
        self.assertEqual(
            self.control()["ingress_assessment_revision_at_send"], assessment["assessment_revision"]
        )
        for key in (
            "assessment_revision",
            "policy_version",
            "episode_generation",
            "reason",
            "deadline",
            "recovery_cutoff_epoch",
            "recovery_cutoff_sequence",
            "source_failure_epoch",
            "source_failure_sequence",
        ):
            self.assertEqual(cause[key], assessment[key])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM provider_stop_requests"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.begin())

    def test_recovery_before_reservation_suppresses_even_due_cause(self):
        self.state.telegram_ingress_assessments.assess(self.job.job_id, now=self.now)
        self.poll(4, succeeded=True, at=self.now)
        self.assertIsNone(self.begin())
        self.assertIsNone(self.control()["send_started_at"])
        self.assertIsNone(self.cause())

    def test_recovery_after_reservation_preserves_cause_and_sender_owner(self):
        self.assertIsNotNone(self.begin())
        cause = self.cause()
        assert cause is not None
        retained = dict(cause), dict(self.control())
        self.poll(4, succeeded=True, at=self.now)
        self.state.telegram_ingress_assessments.assess(self.job.job_id, now=self.now)
        cause = self.cause()
        assert cause is not None
        self.assertEqual((dict(cause), dict(self.control())), retained)
        self.assertIsNone(self.control()["owner_quiesced_at"])

    def test_not_due_writes_no_fence_or_cause(self):
        self.now = self.accepted_at + timedelta(seconds=29)
        self.assertIsNone(self.begin())
        self.assertIsNone(self.control()["send_started_at"])
        self.assertIsNone(self.cause())

    def test_wrong_authority_or_expired_active_proof_cannot_reserve(self):
        self.assertIsNone(self.begin(token="example-wrong-lease"))
        for proof in (
            replace(self.proof(), observed_monotonic=time.monotonic() - 6),
            replace(self.proof(), turn_id="example-other-turn"),
            replace(self.proof(), root="/home/example/other"),
        ):
            self.assertIsNone(self.begin(proof=proof))
        self.assertIsNone(self.control()["send_started_at"])
        self.assertIsNone(self.cause())

    def test_prior_native_fence_cannot_acquire_ingress_provenance(self):
        self.state.codex_controls.begin_interrupt(
            job_id=self.job.job_id,
            source="protective",
            proof=self.proof(),
            validated_root=self.root,
            invocation_token=self.token,
            now=self.now,
        )
        before = dict(self.control())
        self.assertIsNone(self.begin())
        self.assertEqual(dict(self.control()), before)
        self.assertIsNone(self.cause())
        with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE codex_turn_controls SET ingress_assessment_revision_at_send=1 WHERE job_id=?",
                (self.job.job_id,),
            )

    def test_cause_write_failure_rolls_back_fence_and_assessment(self):
        before = dict(self.control())
        self.state._connection.execute(
            """CREATE TEMP TRIGGER example_refuse_ingress_cause BEFORE INSERT ON codex_ingress_interrupt_causes
               BEGIN SELECT RAISE(ABORT, 'example cause fault'); END"""
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "example cause fault"):
            self.begin()
        self.assertEqual(dict(self.control()), before)
        self.assertIsNone(self.state.telegram_ingress_assessments.read(self.job.job_id))
        self.assertIsNone(self.cause())

    def test_cause_and_parent_discriminator_cannot_be_replaced_or_reset(self):
        self.assertIsNotNone(self.begin())
        for sql in (
            "UPDATE codex_ingress_interrupt_causes SET episode_generation=episode_generation+1 WHERE job_id=?",
            "DELETE FROM codex_ingress_interrupt_causes WHERE job_id=?",
            "UPDATE codex_turn_controls SET ingress_assessment_revision_at_send=NULL WHERE job_id=?",
        ):
            with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
                self.state._connection.execute(sql, (self.job.job_id,))

    def test_exact_ingress_claim_consumes_existing_budget_before_read(self):
        self.indeterminate()
        for attempt in range(1, 4):
            claim = self.claim(now=self.now + timedelta(seconds=31 * (attempt - 1)))
            assert claim is not None
            self.assertEqual(claim["late_read_attempts"], attempt)
            self.assertIsNotNone(claim["read_claim_token"])
            self.state.codex_controls.finish_late_read(self.job.job_id, claim["read_claim_token"])
        self.assertIsNone(self.claim(now=self.now + timedelta(seconds=200)))
        self.assertEqual(self.control()["late_read_attempts"], 3)
        self.assertIsNone(
            self.state.codex_controls.claim_late_read("example-other-maintainer", now=self.now)
        )

    def test_ingress_claim_can_reserve_without_fabricating_owner_stop(self):
        self.indeterminate()
        claim = self.claim()
        assert claim is not None
        self.assertIsNotNone(
            self.state.codex_ingress_control.begin_interrupt(
                job_id=self.job.job_id,
                proof=self.proof(),
                validated_root=self.root,
                read_claim_token=claim["read_claim_token"],
                now=self.now,
            )
        )
        self.assertEqual(self.control()["interrupt_source"], "late")
        self.assertIsNone(self.control()["stop_request_id"])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM provider_stop_requests"
            ).fetchone()[0],
            0,
        )

    def test_ordinary_late_source_still_requires_real_stop(self):
        self.indeterminate()
        claim = self.claim()
        assert claim is not None
        self.assertIsNone(
            self.state.codex_controls.begin_interrupt(
                job_id=self.job.job_id,
                source="late",
                proof=self.proof(),
                validated_root=self.root,
                read_claim_token=claim["read_claim_token"],
                now=self.now,
            )
        )
        self.assertIsNone(self.control()["send_started_at"])

    def test_recovery_after_claim_cannot_interrupt_or_replenish_budget(self):
        self.indeterminate()
        claim = self.claim()
        assert claim is not None
        self.poll(4, succeeded=True, at=self.now)
        self.assertIsNone(
            self.state.codex_ingress_control.begin_interrupt(
                job_id=self.job.job_id,
                proof=self.proof(),
                validated_root=self.root,
                read_claim_token=claim["read_claim_token"],
                now=self.now,
            )
        )
        self.assertEqual(self.control()["late_read_attempts"], 1)
        self.assertIsNone(self.control()["send_started_at"])

    def test_unknown_ingress_does_not_gain_authority_from_provider_or_polling(self):
        other = fixtures.TelegramTurnProvenanceTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        job, _ = other.enqueue()
        _, token = other.accept(job)
        root = str(other.harness.root)
        self.assertIsNone(
            other.state.codex_ingress_control.begin_interrupt(
                job_id=job.job_id,
                proof=ActiveTurnProof("example-thread", "example-turn", root, time.monotonic()),
                validated_root=root,
                invocation_token=token,
                now=self.now,
            )
        )
        self.assertIsNone(other.state.telegram_ingress_assessments.read(job.job_id))
        other.state.mark_provider_job_indeterminate(
            job.job_id, token, error_code="example-loss", error_detail="Example uncertainty"
        )
        self.assertIsNone(
            other.state.codex_ingress_control.claim_read(job.job_id, "example-reader", now=self.now)
        )
        row = other.state.codex_controls.read(job.job_id)
        assert row is not None
        self.assertEqual(row["late_read_attempts"], 0)

    def test_invalid_authority_selections_and_nested_call_leave_no_effects(self):
        for tokens in ({}, {"invocation_token": self.token, "read_claim_token": "example-claim"}):
            with self.assertRaises(StateError):
                self.state.codex_ingress_control.begin_interrupt(
                    job_id=self.job.job_id,
                    proof=self.proof(),
                    validated_root=self.root,
                    now=self.now,
                    **tokens,
                )
        with self.state._immediate_transaction():
            with self.assertRaisesRegex(StateError, "cannot nest"):
                self.begin()
            self.assertTrue(self.state._connection.in_transaction)
        self.assertIsNone(self.cause())
        self.assertIsNone(self.state.telegram_ingress_assessments.read(self.job.job_id))

    def test_expired_invocation_and_binding_drift_refuse_claim_and_send(self):
        db = self.state._connection
        with self.state._immediate_transaction():
            db.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
                (self.now.isoformat(), self.job.job_id),
            )
        self.assertIsNone(self.begin())
        self.indeterminate()
        with self.state._immediate_transaction():
            db.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (self.job.session_id,),
            )
        self.assertIsNone(self.claim())
        self.assertEqual(self.control()["late_read_attempts"], 0)
        self.assertIsNone(self.cause())

    def test_deadline_and_proof_rechecked_after_assessment_before_reservation(self):
        original = self.state.telegram_ingress_assessments.assess_in_transaction
        clock = [10.0]

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = 16.0
            return result

        with (
            patch(
                "hermes_codex_router.codex_turn_controls.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch.object(
                self.state.telegram_ingress_assessments,
                "assess_in_transaction",
                side_effect=delayed,
            ),
        ):
            self.assertIsNone(
                self.state.codex_ingress_control.begin_interrupt(
                    job_id=self.job.job_id,
                    proof=replace(self.proof(), observed_monotonic=10.0),
                    validated_root=self.root,
                    invocation_token=self.token,
                    send_deadline=15.0,
                    now=self.now,
                )
            )
        self.assertIsNone(self.cause())
        self.assertIsNone(self.control()["send_started_at"])

    def test_fence_fault_and_commit_fault_return_no_authority_and_rollback(self):
        db = self.state._connection
        before = dict(self.control())
        db.execute("""CREATE TEMP TRIGGER example_fence_fault BEFORE UPDATE ON codex_turn_controls
            WHEN NEW.send_started_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'example fence fault'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "example fence fault"):
            self.begin()
        db.execute("DROP TRIGGER example_fence_fault")

        def deny_commit(action, argument, _second, _database, _trigger):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT"
                else sqlite3.SQLITE_OK
            )

        db.set_authorizer(deny_commit)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.begin()
        finally:
            db.set_authorizer(None)
        self.assertFalse(db.in_transaction)
        self.assertEqual(dict(self.control()), before)
        self.assertIsNone(self.cause())
        self.assertIsNone(self.state.telegram_ingress_assessments.read(self.job.job_id))

    def test_peer_sees_cause_and_fence_only_after_successful_commit(self):
        with closing(
            HubState.open_existing(
                self.fixture.harness.config.state_path, codex_permission_profile=None
            )
        ) as peer:
            reserve = self.state.codex_controls._reserve_in_transaction

            def inspect(*args, **kwargs):
                token = reserve(*args, **kwargs)
                self.assertIsNone(peer.codex_ingress_control.read_cause(self.job.job_id))
                row = peer.codex_controls.read(self.job.job_id)
                assert row is not None
                self.assertIsNone(row["send_started_at"])
                return token

            with patch.object(
                self.state.codex_controls, "_reserve_in_transaction", side_effect=inspect
            ):
                self.assertIsNotNone(self.begin())
            self.assertIsNotNone(peer.codex_ingress_control.read_cause(self.job.job_id))

    def test_malformed_retained_assessment_refuses_without_new_authority(self):
        self.state.telegram_ingress_assessments.assess(self.job.job_id, now=self.now)
        # Simulate genuinely damaged persisted clock evidence, not a naive caller.
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """UPDATE codex_telegram_ingress_assessments
                SET assessment_revision=assessment_revision+1, last_assessed_at='example-invalid-clock'
                WHERE job_id=?""",
                (self.job.job_id,),
            )
        before = dict(self.state.telegram_ingress_assessments.read(self.job.job_id))
        with self.assertRaises(StateError):
            self.begin()
        self.assertEqual(
            dict(self.state.telegram_ingress_assessments.read(self.job.job_id)), before
        )
        self.assertIsNone(self.cause())
        self.assertIsNone(self.control()["send_started_at"])

    def test_expired_replaced_claim_and_spacing_preserve_shared_allowance(self):
        self.indeterminate()
        first = self.claim()
        assert first is not None
        self.assertIsNone(self.claim(now=self.now + timedelta(seconds=29)))
        second = self.claim(now=self.now + timedelta(seconds=31))
        assert second is not None
        self.now += timedelta(seconds=31)
        self.assertIsNone(
            self.state.codex_ingress_control.begin_interrupt(
                job_id=self.job.job_id,
                proof=self.proof(),
                validated_root=self.root,
                read_claim_token=first["read_claim_token"],
                now=self.now,
            )
        )
        self.state.codex_controls.finish_late_read(self.job.job_id, first["read_claim_token"])
        self.assertEqual(self.control()["read_claim_token"], second["read_claim_token"])
        self.assertEqual(self.control()["late_read_attempts"], 2)

    def test_later_real_stop_preserves_claim_budget_schedule_and_takes_precedence(self):
        self.indeterminate()
        claim = self.claim()
        assert claim is not None
        before = dict(self.control())
        request_id, _, _ = self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        after = dict(self.control())
        self.assertEqual(after["stop_request_id"], request_id)
        for key in (
            "late_read_attempts",
            "next_late_read_at",
            "read_claim_token",
            "read_claim_owner",
            "read_claim_expires_at",
        ):
            self.assertEqual(after[key], before[key])
        self.state.codex_controls.finish_late_read(self.job.job_id, claim["read_claim_token"])
        self.now += timedelta(seconds=31)
        self.assertIsNone(self.claim())
        stop_claim = self.state.codex_controls.claim_late_read("example-stop-reader", now=self.now)
        assert stop_claim is not None
        self.assertEqual(stop_claim["late_read_attempts"], 2)
        self.assertIsNotNone(
            self.state.codex_controls.begin_interrupt(
                job_id=self.job.job_id,
                source="late",
                proof=self.proof(),
                validated_root=self.root,
                read_claim_token=stop_claim["read_claim_token"],
                now=self.now,
            )
        )
        self.assertIsNone(self.cause())

    def test_stop_arriving_after_ingress_claim_defers_fence_to_real_stop(self):
        self.indeterminate()
        claim = self.claim()
        assert claim is not None
        self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        self.assertIsNone(
            self.state.codex_ingress_control.begin_interrupt(
                job_id=self.job.job_id,
                proof=self.proof(),
                validated_root=self.root,
                read_claim_token=claim["read_claim_token"],
                now=self.now,
            )
        )
        self.assertIsNone(self.cause())
        self.assertEqual(self.control()["late_read_attempts"], 1)
        self.assertIsNotNone(
            self.state.codex_controls.begin_interrupt(
                job_id=self.job.job_id,
                source="late",
                proof=self.proof(),
                validated_root=self.root,
                read_claim_token=claim["read_claim_token"],
                now=self.now,
            )
        )

    def test_real_stop_on_current_invocation_precedes_ingress_send(self):
        self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        self.assertIsNone(self.begin())
        self.assertIsNone(self.cause())
        self.assertIsNotNone(
            self.state.codex_controls.begin_interrupt(
                job_id=self.job.job_id,
                source="live",
                proof=self.proof(),
                validated_root=self.root,
                invocation_token=self.token,
                now=self.now,
            )
        )

    def test_ingress_fence_excludes_subsequent_stop_and_native_sends(self):
        owner = self.begin()
        assert owner is not None
        self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        for source in ("protective", "live", "permission_drift"):
            self.assertIsNone(
                self.state.codex_controls.begin_interrupt(
                    job_id=self.job.job_id,
                    source=source,
                    proof=self.proof(),
                    validated_root=self.root,
                    invocation_token=self.token,
                    now=self.now,
                )
            )
        self.state.codex_controls.finish_interrupt(
            self.job.job_id, owner, outcome="matched_ack", send_path_quiesced=True, now=self.now
        )
        self.assertIsNone(self.begin())
        self.assertEqual(self.control()["interrupt_outcome"], "matched_ack")
        self.assertIsNotNone(self.cause())

    def test_replacement_and_rowid_attacks_preserve_frozen_cause_without_recursive_triggers(self):
        self.assertIsNotNone(self.begin())
        cause = self.cause()
        assert cause is not None
        before = dict(cause)
        db = self.state._connection
        db.execute("PRAGMA recursive_triggers=OFF")
        rowid = db.execute("SELECT rowid FROM codex_ingress_interrupt_causes").fetchone()[0]
        for values in (before, before | {"rowid": rowid, "job_id": "example-other-target"}):
            with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
                db.execute(
                    f"INSERT OR REPLACE INTO codex_ingress_interrupt_causes ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
        with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
            db.execute("UPDATE OR REPLACE codex_ingress_interrupt_causes SET rowid=rowid+1")
        self.assertEqual(dict(self.cause()), before)

    def test_claim_fault_rolls_back_allowance_and_assessment(self):
        self.indeterminate()
        self.state._connection.execute("""CREATE TEMP TRIGGER example_claim_fault BEFORE UPDATE ON codex_turn_controls
            WHEN NEW.late_read_attempts>OLD.late_read_attempts BEGIN SELECT RAISE(ABORT, 'example claim fault'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "example claim fault"):
            self.claim()
        self.assertEqual(self.control()["late_read_attempts"], 0)
        self.assertIsNone(self.control()["read_claim_token"])
        self.assertIsNone(self.state.telegram_ingress_assessments.read(self.job.job_id))

    def test_invocation_expiry_during_assessment_refuses_still_fresh_proof(self):
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
                ((self.now + timedelta(milliseconds=500)).isoformat(), self.job.job_id),
            )
        with patch(
            "hermes_codex_router.codex_ingress_control._now",
            side_effect=(self.now, self.now + timedelta(seconds=1)),
        ):
            self.assertIsNone(
                self.state.codex_ingress_control.begin_interrupt(
                    job_id=self.job.job_id,
                    proof=self.proof(),
                    validated_root=self.root,
                    invocation_token=self.token,
                )
            )
        self.assertIsNone(self.control()["send_started_at"])
        self.assertIsNone(self.cause())

    def test_read_claim_expiry_during_assessment_refuses_still_fresh_proof(self):
        self.indeterminate()
        claim = self.claim()
        assert claim is not None
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE codex_turn_controls SET read_claim_expires_at=? WHERE job_id=?",
                ((self.now + timedelta(milliseconds=500)).isoformat(), self.job.job_id),
            )
        with patch(
            "hermes_codex_router.codex_ingress_control._now",
            side_effect=(self.now, self.now + timedelta(seconds=1)),
        ):
            self.assertIsNone(
                self.state.codex_ingress_control.begin_interrupt(
                    job_id=self.job.job_id,
                    proof=self.proof(),
                    validated_root=self.root,
                    read_claim_token=claim["read_claim_token"],
                )
            )
        self.assertEqual(self.control()["late_read_attempts"], 1)
        self.assertIsNone(self.cause())

    def test_recovery_and_new_episode_never_refill_exhausted_allowance(self):
        self.indeterminate()
        for offset in (0, 31, 62):
            claim = self.claim(now=self.now + timedelta(seconds=offset))
            assert claim is not None
            self.state.codex_controls.finish_late_read(self.job.job_id, claim["read_claim_token"])
        self.now += timedelta(seconds=63)
        self.poll(4, succeeded=True, at=self.now)
        self.assertIsNone(self.claim())
        self.now += timedelta(seconds=1)
        for sequence in (5, 6, 7):
            self.poll(sequence, succeeded=False, at=self.now)
        self.now += timedelta(seconds=31)
        self.assertIsNone(self.claim())
        assessment = self.state.telegram_ingress_assessments.read(self.job.job_id)
        assert assessment is not None
        self.assertEqual(assessment["episode_generation"], 2)
        self.assertEqual(self.control()["late_read_attempts"], 3)

    def test_unknown_commentary_and_consent_do_not_create_or_suppress_ingress_cause(self):
        self.poll(4, succeeded=True, at=self.now)
        db = self.state._connection
        with self.state._immediate_transaction():
            db.execute(
                "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                (f"root:{self.root}", self.job.topic_id),
            )
            sequence = db.execute(
                """INSERT INTO provider_visible_items
                (job_id,item_id,phase,visible_text,created_at)
                VALUES (?,'example-commentary','commentary','Example progress',?)""",
                (self.job.job_id, self.now.isoformat()),
            ).lastrowid
            db.execute(
                """INSERT INTO provider_progress_deliveries
                (progress_id,item_sequence,job_id,sender_agent_id,chat_id,thread_id,telegram_html,status,available_at,created_at,updated_at)
                VALUES ('example-unknown-progress',?,?,'codex',?,?,'Example progress','unknown',?,?,?)""",
                (
                    sequence,
                    self.job.job_id,
                    self.job.chat_id,
                    self.control()["thread_id"],
                    self.now.isoformat(),
                    self.now.isoformat(),
                    self.now.isoformat(),
                ),
            )
        self.assertIsNone(self.begin())
        self.assertIsNone(self.cause())
        preview = self.state.preview_delivery_control(
            "progress_delivery", "example-unknown-progress"
        )
        self.state.reconcile_delivery_control(
            "progress_delivery",
            "example-unknown-progress",
            expected_snapshot=preview.snapshot,
            accept_unconfirmed_delivery=True,
        )
        self.assertIsNone(self.begin())
        self.now += timedelta(seconds=1)
        for sequence in (5, 6, 7):
            self.poll(sequence, succeeded=False, at=self.now)
        self.now += timedelta(seconds=31)
        self.assertIsNotNone(self.begin())
        self.assertIsNotNone(self.cause())
        self.assertEqual(
            db.execute("SELECT status FROM provider_progress_deliveries").fetchone()[0], "unknown"
        )
        self.assertEqual(
            db.execute("SELECT count(*) FROM telegram_delivery_control_dispositions").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
