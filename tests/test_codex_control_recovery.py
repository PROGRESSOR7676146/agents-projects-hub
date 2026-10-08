"""Protective interruption never substitutes acceptance, terminality or replay."""

from __future__ import annotations

import unittest
from pathlib import Path

from hermes_codex_router.codex_appserver import StoredTurnOutcome, TurnResult
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

    def interrupt_turn(self, *, thread_id, turn_id, deadline=None):
        self.calls.append(("interrupt", thread_id, turn_id))


class ControlRecoveryTests(unittest.TestCase):
    def observe(self, client, *, authorize=lambda: True):
        return observe_after_control_loss(
            client,
            thread_id="example-thread",
            turn_id="example-turn",
            root=Path("/home/example/project"),
            may_interrupt=authorize,
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
            may_interrupt=guard,
            on_interrupt_event=events.append,
        )
        self.assertEqual(outcome.status, "active")
        self.assertEqual(events, [])
        self.assertEqual([call[0] for call in client.calls], ["read"])

    def test_event_failure_before_send_refuses_and_after_ack_preserves_terminal_read(self):
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
                    may_interrupt=lambda: True,
                    on_interrupt_event=record,
                )
                self.assertEqual(
                    outcome.status, "active" if failed_event == "attempted" else "interrupted"
                )
                self.assertEqual(
                    [call[0] for call in client.calls],
                    ["read"] if failed_event == "attempted" else ["read", "interrupt", "read"],
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
