"""Protective interruption never substitutes acceptance, terminality or replay."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.codex_appserver import RpcRejectedError, StoredTurnOutcome, TurnResult
from hermes_codex_router.codex_control_recovery import observe_after_control_loss


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

    def interrupt_turn(self, *, thread_id, turn_id, deadline=None):
        self.calls.append(("interrupt", thread_id, turn_id))


class ControlRecoveryTests(unittest.TestCase):
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
