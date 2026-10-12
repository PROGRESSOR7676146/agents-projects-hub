"""Protective interruption never substitutes acceptance, terminality or replay."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_router.codex_appserver import RpcRejectedError, StoredTurnOutcome, TurnResult
from hermes_codex_router.codex_control_recovery import observe_after_control_loss
from hermes_codex_router.codex_rpc import RpcSendDeadlineError
from hermes_codex_router.codex_turn_controls import ActiveTurnProof
from hermes_codex_router.root_blockers import persistent_root_blocker
from tests import test_codex_turn_controls as fixtures


class Client:
    def __init__(self, outcomes: list[StoredTurnOutcome | Exception]) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[tuple[str, str, str]] = []

    def read_turn_outcome(self, *, thread_id, turn_id, cwd, deadline=None):
        self.calls.append(("read", thread_id, turn_id))
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass

    def interrupt_turn(self, *, thread_id, turn_id, deadline=None, send_start_deadline=None):
        self.calls.append(("interrupt", thread_id, turn_id))


class ControlRecoveryTests(unittest.TestCase):
    def test_original_proof_deadline_and_post_call_expiry_retain_unknown_owner(self):
        fixture = fixtures.CodexTurnControlJournalTests()
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups()))
        fixture.journal.record_turn(fixture.job_id, fixture.token, "example-turn")
        clock = [0.0]
        client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("active")])
        calls = []

        def begin(proof, deadline):
            owner = fixture.state.codex_controls.begin_interrupt(
                job_id=fixture.job_id,
                source="protective",
                proof=proof,
                validated_root=str(fixture.root),
                invocation_token=fixture.token,
                send_deadline=deadline,
            )
            clock[0] = 4.0
            return owner

        def interrupt(**kwargs):
            calls.append(kwargs)
            # Transport queue expiry happens after the client method was called.
            clock[0] = 6.0
            raise RpcSendDeadlineError()

        client.interrupt_turn = interrupt
        with (
            patch(
                "hermes_codex_router.codex_control_recovery.time",
                SimpleNamespace(monotonic=lambda: clock[0]),
            ),
            patch(
                "hermes_codex_router.codex_turn_controls.time",
                SimpleNamespace(monotonic=lambda: clock[0]),
            ),
        ):
            outcome = observe_after_control_loss(
                client,
                thread_id="example-thread",
                turn_id="example-turn",
                root=fixture.root,
                begin_interrupt=begin,
                finish_interrupt=lambda owner, result: (
                    fixture.state.codex_controls.finish_interrupt(
                        fixture.job_id,
                        owner,
                        outcome=result,
                        send_path_quiesced=result != "unknown",
                    )
                ),
            )
            self.assertIsNone(
                fixture.state.codex_controls.begin_interrupt(
                    job_id=fixture.job_id,
                    source="protective",
                    proof=ActiveTurnProof(
                        "example-thread", "example-turn", str(fixture.root), clock[0]
                    ),
                    validated_root=str(fixture.root),
                    invocation_token=fixture.token,
                )
            )
        self.assertEqual(outcome.status, "active")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["send_start_deadline"], 5.0)
        self.assertEqual(calls[0]["deadline"], 9.0)
        row = fixture.state.codex_controls.read(fixture.job_id)
        assert row is not None
        self.assertIsNotNone(row["send_started_at"])
        self.assertEqual(row["interrupt_outcome"], "unknown")
        self.assertIsNone(row["owner_quiesced_at"])
        job = fixture.state.get_provider_job(fixture.job_id)
        with fixture.state._immediate_transaction():
            self.assertIsNotNone(
                persistent_root_blocker(fixture.state._connection, topic_id=job.topic_id)
            )

    def test_matched_reply_settlement_retries_only_contention_not_rpc(self):
        client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("interrupted")])
        attempts = []

        def finish(owner, outcome):
            attempts.append((owner, outcome))
            if len(attempts) < 3:
                error = sqlite3.OperationalError("Example contention")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error

        result = observe_after_control_loss(
            client,
            thread_id="example-thread",
            turn_id="example-turn",
            root=Path("/home/example/project"),
            begin_interrupt=lambda proof, deadline: "example-owner",
            finish_interrupt=finish,
        )
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(attempts, [("example-owner", "matched_ack")] * 3)
        self.assertEqual([call[0] for call in client.calls], ["read", "interrupt", "read"])

    def test_busy_guard_propagates_before_any_send_or_settlement(self):
        client = Client([StoredTurnOutcome("active")])
        error = sqlite3.OperationalError("Example contention")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY

        def begin(proof, deadline):
            raise error

        with self.assertRaises(sqlite3.OperationalError):
            observe_after_control_loss(
                client,
                thread_id="example-thread",
                turn_id="example-turn",
                root=Path("/home/example/project"),
                begin_interrupt=begin,
                finish_interrupt=lambda owner, outcome: self.fail("No send owner exists"),
            )
        self.assertEqual([call[0] for call in client.calls], ["read"])

    def test_guard_exhausting_rpc_budget_never_sends_late_interrupt(self):
        clock = [0.0]
        settlements = []
        client = Client([StoredTurnOutcome("active")])

        def slow_guard(proof, deadline):
            self.assertEqual(deadline, 15.0)
            clock[0] = 16.0
            return "example-persisted-owner"

        with patch(
            "hermes_codex_router.codex_control_recovery.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            observe_after_control_loss(
                client,
                thread_id="example-thread",
                turn_id="example-turn",
                root=Path("/home/example/project"),
                begin_interrupt=slow_guard,
                finish_interrupt=lambda owner, outcome: settlements.append((owner, outcome)),
            )
        self.assertEqual(client.calls, [("read", "example-thread", "example-turn")])
        self.assertEqual(settlements, [("example-persisted-owner", "not_sent")])

    def test_commit_delay_expiring_active_proof_never_sends_with_remaining_deadline(self):
        clock = [0.0]
        settlements = []
        events = []
        client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("active")])

        def committed_guard(proof, deadline):
            self.assertEqual(proof.observed_monotonic, 0.0)
            self.assertEqual(deadline, 15.0)
            clock[0] = 5.001
            return "example-persisted-owner"

        with patch(
            "hermes_codex_router.codex_control_recovery.time",
            SimpleNamespace(monotonic=lambda: clock[0]),
        ):
            outcome = observe_after_control_loss(
                client,
                thread_id="example-thread",
                turn_id="example-turn",
                root=Path("/home/example/project"),
                begin_interrupt=committed_guard,
                finish_interrupt=lambda owner, result: settlements.append((owner, result)),
                on_interrupt_event=events.append,
            )
        self.assertEqual(outcome.status, "active")
        self.assertEqual([call[0] for call in client.calls], ["read", "read"])
        self.assertEqual(settlements, [("example-persisted-owner", "not_sent")])
        self.assertNotIn("attempted", events)

    def test_committed_guard_at_five_second_proof_boundary_can_send_once(self):
        clock = [0.0]
        settlements = []
        client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("interrupted")])

        def committed_guard(proof, deadline):
            clock[0] = 5.0
            return "example-persisted-owner"

        with patch(
            "hermes_codex_router.codex_control_recovery.time",
            SimpleNamespace(monotonic=lambda: clock[0]),
        ):
            outcome = observe_after_control_loss(
                client,
                thread_id="example-thread",
                turn_id="example-turn",
                root=Path("/home/example/project"),
                begin_interrupt=committed_guard,
                finish_interrupt=lambda owner, result: settlements.append((owner, result)),
            )
        self.assertEqual(outcome.status, "interrupted")
        self.assertEqual([call[0] for call in client.calls], ["read", "interrupt", "read"])
        self.assertEqual(settlements, [("example-persisted-owner", "matched_ack")])

    def test_expired_post_commit_proof_retains_real_fence_and_native_root_exclusion(self):
        fixture = fixtures.CodexTurnControlJournalTests()
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups()))
        fixture.journal.record_turn(fixture.job_id, fixture.token, "example-turn")
        clock = [0.0]
        client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("active")])

        def begin(proof, deadline):
            owner = fixture.state.codex_controls.begin_interrupt(
                job_id=fixture.job_id,
                source="protective",
                proof=proof,
                validated_root=str(fixture.root),
                invocation_token=fixture.token,
                send_deadline=deadline,
            )
            self.assertIsNotNone(owner)
            # Time spent exiting the committed transaction is independent of
            # the RPC deadline and may expire the already validated proof.
            clock[0] = 6.0
            return owner

        with (
            patch(
                "hermes_codex_router.codex_control_recovery.time",
                SimpleNamespace(monotonic=lambda: clock[0]),
            ),
            patch(
                "hermes_codex_router.codex_turn_controls.time",
                SimpleNamespace(monotonic=lambda: clock[0]),
            ),
        ):
            outcome = observe_after_control_loss(
                client,
                thread_id="example-thread",
                turn_id="example-turn",
                root=fixture.root,
                begin_interrupt=begin,
                finish_interrupt=lambda owner, result: (
                    fixture.state.codex_controls.finish_interrupt(
                        fixture.job_id,
                        owner,
                        outcome=result,
                        send_path_quiesced=result != "unknown",
                    )
                ),
            )
            row = fixture.state.codex_controls.read(fixture.job_id)
            assert row is not None
            self.assertIsNotNone(row["send_started_at"])
            self.assertEqual(row["interrupt_outcome"], "not_sent")
            self.assertIsNotNone(row["owner_quiesced_at"])
            self.assertIsNone(
                fixture.state.codex_controls.begin_interrupt(
                    job_id=fixture.job_id,
                    source="protective",
                    proof=ActiveTurnProof(
                        "example-thread", "example-turn", str(fixture.root), clock[0]
                    ),
                    validated_root=str(fixture.root),
                    invocation_token=fixture.token,
                )
            )
        self.assertEqual(outcome.status, "active")
        self.assertEqual([call[0] for call in client.calls], ["read", "read"])
        fixture.state.mark_provider_job_indeterminate(
            fixture.job_id,
            fixture.token,
            error_code="example-control-loss",
            error_detail="Fictional active turn without terminal proof",
        )
        job = fixture.state.get_provider_job(fixture.job_id)
        with fixture.state._immediate_transaction():
            self.assertIsNotNone(
                persistent_root_blocker(fixture.state._connection, topic_id=job.topic_id)
            )

    def test_shared_authority_records_matched_reply_and_refuses_second_send(self):
        owners = []
        settlements = []

        def begin(proof, deadline):
            self.assertEqual((proof.thread_id, proof.turn_id), ("example-thread", "example-turn"))
            if owners:
                return None
            owners.append("example-owner")
            return owners[-1]

        for after in ("active", "interrupted"):
            client = Client([StoredTurnOutcome("active"), StoredTurnOutcome(after)])
            observe_after_control_loss(
                client,
                thread_id="example-thread",
                turn_id="example-turn",
                root=Path("/home/example/project"),
                begin_interrupt=begin,
                finish_interrupt=lambda owner, outcome: settlements.append((owner, outcome)),
            )
            self.assertEqual(
                [call[0] for call in client.calls],
                ["read", "interrupt", "read"] if after == "active" else ["read"],
            )
        self.assertEqual(settlements, [("example-owner", "matched_ack")])

    def test_explicit_rejection_and_transport_loss_keep_distinct_sender_evidence(self):
        for error, expected in (
            (RpcRejectedError("Example rejected"), "matched_rejection"),
            (EOFError("Example lost response"), "unknown"),
        ):
            with self.subTest(outcome=expected):
                client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("interrupted")])
                settlements = []

                def interrupt(**kwargs):
                    raise error

                client.interrupt_turn = interrupt
                outcome = observe_after_control_loss(
                    client,
                    thread_id="example-thread",
                    turn_id="example-turn",
                    root=Path("/home/example/project"),
                    begin_interrupt=lambda proof, deadline: "example-owner",
                    finish_interrupt=lambda owner, outcome: settlements.append(outcome),
                )
                self.assertEqual(outcome.status, "interrupted")
                self.assertEqual(settlements, [expected])

    def observe(self, client, *, authorize=lambda: True):
        return observe_after_control_loss(
            client,
            thread_id="example-thread",
            turn_id="example-turn",
            root=Path("/home/example/project"),
            begin_interrupt=lambda proof, deadline: "example-owner" if authorize() else None,
            finish_interrupt=lambda owner, outcome: None,
        )

    def test_active_is_interrupted_once_and_ack_is_not_terminality(self):
        for after in (StoredTurnOutcome("active"), StoredTurnOutcome("unknown")):
            with self.subTest(after=after.status):
                client = Client([StoredTurnOutcome("active"), after])
                self.assertEqual(self.observe(client), after)
                self.assertEqual([call[0] for call in client.calls], ["read", "interrupt", "read"])

    def test_completed_result_is_recovered_without_interrupt(self):
        final = StoredTurnOutcome("completed", TurnResult("Saved final", None, None))
        client = Client([final])
        self.assertEqual(self.observe(client), final)
        self.assertEqual([call[0] for call in client.calls], ["read"])

    def test_unknown_and_missing_exact_observation_never_guess_interrupt(self):
        for outcome in (StoredTurnOutcome("unknown"), OSError("Example lost socket")):
            client = Client([outcome])
            self.assertEqual(self.observe(client).status, "unknown")
            self.assertEqual([call[0] for call in client.calls], ["read"])

    def test_binding_change_after_read_refuses_control(self):
        client = Client([StoredTurnOutcome("active")])
        self.assertEqual(self.observe(client, authorize=lambda: False).status, "active")
        self.assertEqual([call[0] for call in client.calls], ["read"])

    def test_guard_exception_never_records_an_interrupt_attempt(self):
        client = Client([StoredTurnOutcome("active")])
        events = []

        def guard():
            raise OSError("Example invalid binding")

        outcome = observe_after_control_loss(
            client,
            thread_id="example-thread",
            turn_id="example-turn",
            root=Path("/home/example/project"),
            begin_interrupt=lambda proof, deadline: guard(),
            finish_interrupt=lambda owner, outcome: None,
            on_interrupt_event=events.append,
        )
        self.assertEqual(outcome.status, "active")
        self.assertEqual(events, [])
        self.assertEqual([call[0] for call in client.calls], ["read"])

    def test_optional_event_failure_preserves_fenced_control_and_terminal_read(self):
        for failed_event in ("attempted", "acknowledged"):
            with self.subTest(event=failed_event):
                client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("interrupted")])

                def record(event):
                    if event == failed_event:
                        raise OSError("Example unavailable event journal")

                outcome = observe_after_control_loss(
                    client,
                    thread_id="example-thread",
                    turn_id="example-turn",
                    root=Path("/home/example/project"),
                    begin_interrupt=lambda proof, deadline: "example-owner",
                    finish_interrupt=lambda owner, outcome: None,
                    on_interrupt_event=record,
                )
                self.assertEqual(outcome.status, "interrupted")
                self.assertEqual(
                    [call[0] for call in client.calls],
                    ["read", "interrupt", "read"],
                )

    def test_terminal_after_interrupt_has_exact_identity_without_replay(self):
        client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("interrupted")])
        self.assertEqual(self.observe(client).status, "interrupted")
        self.assertEqual(
            client.calls,
            [
                ("read", "example-thread", "example-turn"),
                ("interrupt", "example-thread", "example-turn"),
                ("read", "example-thread", "example-turn"),
            ],
        )

    def test_interrupt_transport_fault_still_allows_one_exact_read(self):
        client = Client([StoredTurnOutcome("active"), StoredTurnOutcome("interrupted")])

        def interrupt(**kwargs):
            client.calls.append(("interrupt", kwargs["thread_id"], kwargs["turn_id"]))
            raise EOFError("Example ambiguous interrupt")

        client.interrupt_turn = interrupt
        self.assertEqual(self.observe(client).status, "interrupted")
        self.assertEqual([call[0] for call in client.calls], ["read", "interrupt", "read"])
