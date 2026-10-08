"""Crash-safe interrupt ownership is separate from exact native terminality."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import unittest
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from hermes_codex_router.codex_turn_controls import ActiveTurnProof, CodexTurnControls
from hermes_codex_router.state import HubState, StateError
from tests import test_codex_turn_controls as fixtures


class CodexControlFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexTurnControlJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.job_id = self.fixture.job_id
        self.token = self.fixture.token
        self.root = str(self.fixture.root)
        self.journal = self.fixture.journal
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.controls = CodexTurnControls(
            self.state._connection, transaction=self.state._immediate_transaction
        )

    def row(self) -> sqlite3.Row:
        row = self.controls.read(self.job_id)
        assert row is not None
        return row

    def proof(self) -> ActiveTurnProof:
        return ActiveTurnProof(
            thread_id="example-thread",
            turn_id="example-turn",
            root=self.root,
            observed_monotonic=time.monotonic(),
        )

    def begin(self, **overrides):
        kwargs: dict[str, Any] = dict(
            job_id=self.job_id,
            source="protective",
            proof=self.proof(),
            validated_root=self.root,
            invocation_token=self.token,
        )
        kwargs.update(overrides)
        return self.controls.begin_interrupt(**kwargs)

    def indeterminate_with_stop(self) -> datetime:
        job = self.state.get_provider_job(self.job_id)
        self.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=999, target_agent_id="codex"
        )
        self.state.mark_provider_job_indeterminate(
            self.job_id, self.token, error_code="example-stream-loss", error_detail="Fictional"
        )
        return datetime.now(timezone.utc)

    def test_send_start_is_durable_and_second_connection_cannot_send(self) -> None:
        owner = self.begin()
        assert owner is not None
        self.assertIsNotNone(owner)
        with closing(
            HubState.open(self.fixture.fixture.config.state_path, codex_permission_profile=None)
        ) as peer:
            controls = CodexTurnControls(peer._connection, transaction=peer._immediate_transaction)
            self.assertIsNone(
                controls.begin_interrupt(
                    job_id=self.job_id,
                    source="permission_drift",
                    proof=self.proof(),
                    validated_root=self.root,
                    invocation_token=self.token,
                )
            )
        row = self.row()
        self.assertEqual(row["send_owner_token_hash"], hashlib.sha256(owner.encode()).hexdigest())
        self.assertEqual(row["interrupt_source"], "protective")
        self.assertIsNotNone(row["send_started_at"])

    def test_never_called_sender_quiesces_without_resetting_fence_or_native_uncertainty(
        self,
    ) -> None:
        owner = self.begin()
        assert owner is not None
        self.controls.finish_interrupt(
            self.job_id,
            owner,
            outcome="not_sent",
            send_path_quiesced=True,
        )
        self.assertIsNotNone(self.row()["owner_quiesced_at"])
        self.assertEqual(self.row()["interrupt_outcome"], "not_sent")
        self.assertIsNone(self.begin(source="permission_drift"))
        saved = self.journal.read(self.job_id)
        assert saved is not None
        self.assertIsNone(saved["completed_text"])
        self.assertIsNone(
            self.state._connection.execute(
                "SELECT 1 FROM provider_turn_terminal_evidence WHERE job_id=?",
                (self.job_id,),
            ).fetchone()
        )
        with self.assertRaises(StateError):
            self.controls.finish_interrupt(
                self.job_id,
                owner,
                outcome="matched_ack",
                send_path_quiesced=True,
            )

    def test_competing_live_and_protective_connections_share_one_send_owner(self) -> None:
        job = self.state.get_provider_job(self.job_id)
        self.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=999, target_agent_id="codex"
        )
        barrier = threading.Barrier(3)
        owners, failures = [], []

        def compete(source):
            try:
                with closing(
                    HubState.open_existing(
                        self.fixture.fixture.config.state_path, codex_permission_profile=None
                    )
                ) as peer:
                    barrier.wait(timeout=2)
                    owners.append(
                        peer.codex_controls.begin_interrupt(
                            job_id=self.job_id,
                            source=source,
                            proof=self.proof(),
                            validated_root=self.root,
                            invocation_token=self.token,
                        )
                    )
            except BaseException as error:
                failures.append(error)

        senders = [
            threading.Thread(target=compete, args=(source,)) for source in ("live", "protective")
        ]
        for sender in senders:
            sender.start()
        barrier.wait(timeout=2)
        for sender in senders:
            sender.join(2)
            self.assertFalse(sender.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(sum(owner is not None for owner in owners), 1)
        self.assertIsNone(self.begin(source="permission_drift"))

    def test_replaced_late_claim_cannot_send_even_with_fresh_exact_proof(self) -> None:
        now = self.indeterminate_with_stop()
        first = self.controls.claim_late_read("example-first", now=now)
        assert first is not None
        second = self.controls.claim_late_read("example-second", now=now + timedelta(seconds=31))
        assert second is not None
        self.assertIsNone(
            self.begin(
                source="late", invocation_token=None, read_claim_token=first["read_claim_token"]
            )
        )
        self.assertIsNotNone(
            self.begin(
                source="late", invocation_token=None, read_claim_token=second["read_claim_token"]
            )
        )

    def test_stale_or_wrong_exact_active_proof_cannot_fence(self) -> None:
        for field, value in (
            ("turn_id", "example-other-turn"),
            ("thread_id", "example-other-thread"),
            ("root", "/home/example/other"),
            ("observed_monotonic", time.monotonic() - 30),
            ("observed_monotonic", time.monotonic() + 30),
            ("observed_monotonic", float("nan")),
        ):
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.begin(proof=replace(self.proof(), **{field: value})))
        self.assertIsNone(self.row()["send_started_at"])

    def test_expired_invocation_authority_cannot_send(self) -> None:
        self.state.heartbeat_provider_job(
            self.job_id,
            self.token,
            now=datetime.now(timezone.utc) - timedelta(seconds=30),
            lease_seconds=1,
        )
        self.assertIsNone(self.begin())

    def test_registry_or_session_binding_drift_cannot_send(self) -> None:
        self.assertIsNone(self.begin(validated_root="/home/example/other"))
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET provider_session_id='example-other-thread'"
            )
        self.assertIsNone(self.begin())

    def test_completed_checkpoint_or_terminal_proof_cannot_authorize_interrupt(self) -> None:
        self.journal.record_completion(self.job_id, self.token, "Fictional saved final")
        self.assertIsNone(self.begin())

    def test_live_source_requires_original_covering_pending_stop(self) -> None:
        self.assertIsNone(self.begin(source="live"))
        job = self.state.get_provider_job(self.job_id)
        stop, _, _ = self.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=999, target_agent_id="codex"
        )
        self.assertEqual(self.row()["stop_request_id"], stop)
        self.assertIsNotNone(self.begin(source="live"))

    def test_unknown_finish_and_age_do_not_quiesce_or_reset_owner(self) -> None:
        owner = self.begin()
        assert owner is not None
        self.controls.finish_interrupt(
            self.job_id, owner, outcome="unknown", send_path_quiesced=False
        )
        row = self.row()
        self.assertEqual(row["interrupt_outcome"], "unknown")
        self.assertIsNone(row["owner_quiesced_at"])
        self.assertIsNone(self.begin())
        with self.assertRaises(StateError):
            self.controls.finish_interrupt(
                self.job_id, owner, outcome="unknown", send_path_quiesced=True
            )

    def test_matched_ack_quiesces_sender_but_does_not_invent_native_terminality(self) -> None:
        owner = self.begin()
        assert owner is not None
        self.controls.finish_interrupt(
            self.job_id, owner, outcome="matched_ack", send_path_quiesced=True
        )
        self.assertIsNotNone(self.row()["owner_quiesced_at"])
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "executing")
        checkpoint = self.journal.read(self.job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["completed_text"])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM provider_turn_terminal_evidence"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.begin())

    def test_wrong_sender_cannot_finish_and_matched_outcome_cannot_change(self) -> None:
        owner = self.begin()
        assert owner is not None
        with self.assertRaises(StateError):
            self.controls.finish_interrupt(
                self.job_id, "example-other-owner", outcome="matched_ack", send_path_quiesced=True
            )
        self.controls.finish_interrupt(
            self.job_id, owner, outcome="matched_rejection", send_path_quiesced=True
        )
        before = dict(self.row())
        with self.assertRaises(StateError):
            self.controls.finish_interrupt(
                self.job_id, owner, outcome="unknown", send_path_quiesced=False
            )
        self.assertEqual(dict(self.row()), before)

    def test_late_read_claim_never_overlaps_productive_invocation(self) -> None:
        job = self.state.get_provider_job(self.job_id)
        self.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=999, target_agent_id="codex"
        )
        self.assertIsNone(self.controls.claim_late_read("example-maintainer"))

    def test_claim_consumes_budget_before_connection_and_spacing_survives_restart(self) -> None:
        now = self.indeterminate_with_stop()
        for attempt in range(1, 4):
            claimed = self.controls.claim_late_read("example-maintainer", now=now)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["late_read_attempts"], attempt)
            self.controls.finish_late_read(self.job_id, claimed["read_claim_token"])
            with closing(
                HubState.open(self.fixture.fixture.config.state_path, codex_permission_profile=None)
            ) as peer:
                controls = CodexTurnControls(
                    peer._connection, transaction=peer._immediate_transaction
                )
                self.assertIsNone(controls.claim_late_read("example-other-maintainer", now=now))
            now += timedelta(seconds=31)
        self.assertIsNone(self.controls.claim_late_read("example-maintainer", now=now))
        self.assertEqual(self.row()["late_read_attempts"], 3)

    def test_only_current_read_claim_can_send_and_expiry_never_clears_send_owner(self) -> None:
        now = self.indeterminate_with_stop()
        claimed = self.controls.claim_late_read("example-maintainer", now=now)
        assert claimed is not None
        token = claimed["read_claim_token"]
        self.assertIsNone(
            self.begin(invocation_token=None, read_claim_token="example-wrong-claim", source="late")
        )
        owner = self.begin(invocation_token=None, read_claim_token=token, source="late")
        self.assertIsNotNone(owner)
        assert owner is not None
        self.controls.finish_late_read(self.job_id, token)
        second = self.controls.claim_late_read(
            "example-next-maintainer", now=now + timedelta(seconds=31)
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIsNone(
            self.begin(
                invocation_token=None, read_claim_token=second["read_claim_token"], source="late"
            )
        )
        self.assertEqual(
            self.row()["send_owner_token_hash"],
            hashlib.sha256(owner.encode()).hexdigest(),
        )
        self.assertIsNone(self.row()["owner_quiesced_at"])

    def test_read_claim_and_invocation_tokens_are_mutually_exclusive(self) -> None:
        with self.assertRaises(StateError):
            self.begin(read_claim_token="example-claim")

    def test_late_claim_cannot_disguise_control_as_another_source(self) -> None:
        now = self.indeterminate_with_stop()
        claimed = self.controls.claim_late_read("example-maintainer", now=now)
        assert claimed is not None
        for source in ("protective", "permission_drift", "live"):
            with self.subTest(source=source), self.assertRaises(StateError):
                self.begin(
                    invocation_token=None,
                    read_claim_token=claimed["read_claim_token"],
                    source=source,
                )
        self.assertIsNone(self.row()["send_started_at"])

    def test_readable_journal_does_not_disclose_raw_sender_finish_authority(self) -> None:
        owner = self.begin()
        assert owner is not None
        row = self.row()
        self.assertNotIn(owner, dict(row).values())
        with self.assertRaises(StateError):
            self.controls.finish_interrupt(
                self.job_id,
                row["send_owner_token_hash"],
                outcome="matched_ack",
                send_path_quiesced=True,
            )
        self.assertIsNone(self.row()["owner_quiesced_at"])

    def test_storage_cannot_quiesce_sender_without_a_matched_response(self) -> None:
        self.begin()
        with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE codex_turn_controls SET owner_quiesced_at=? WHERE job_id=?",
                (datetime.now(timezone.utc).isoformat(), self.job_id),
            )

    def test_invocation_expiry_is_rechecked_after_acquiring_sqlite_lock(self) -> None:
        start = datetime.now(timezone.utc)
        self.state.heartbeat_provider_job(self.job_id, self.token, now=start, lease_seconds=1)
        acquired = False

        @contextmanager
        def transaction():
            nonlocal acquired
            with self.state._immediate_transaction():
                acquired = True
                yield

        self.controls = CodexTurnControls(self.state._connection, transaction=transaction)
        with patch(
            "hermes_codex_router.codex_turn_controls._now",
            side_effect=lambda _: start + timedelta(seconds=2 if acquired else 0),
        ):
            self.assertIsNone(self.begin())
        self.assertIsNone(self.row()["send_started_at"])

    def test_late_read_does_not_consume_budget_for_an_already_resolved_target(self) -> None:
        now = self.indeterminate_with_stop()
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_execution_checkpoints SET completed_text='Fictional final' WHERE job_id=?",
                (self.job_id,),
            )
        self.assertIsNone(self.controls.claim_late_read("example-maintainer", now=now))
        self.assertEqual(self.row()["late_read_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
