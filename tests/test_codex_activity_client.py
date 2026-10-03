from __future__ import annotations

import tempfile
import unittest
from collections import deque
from dataclasses import asdict
from pathlib import Path

from hermes_codex_router.codex_activity import CodexActivityEvent
from hermes_codex_router.codex_appserver import CodexAppServerClient, CodexTurnError, RpcError


class Transport:
    def __init__(self, incoming: list[dict]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[dict] = []

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def receive(self, *, timeout: float | None = None) -> dict:
        if not self.incoming:
            raise EOFError("fake transport exhausted")
        return self.incoming.popleft()

    def close(self) -> None:
        pass


def approval(
    request_id: str | int = 71, *, thread: str = "thread-example", turn: str = "turn-example"
) -> dict:
    return {
        "id": request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "itemId": "item-example",
            "command": ["secret-argument"],
            "reason": "private-reason",
            "cwd": "/home/example/private",
        },
    }


def resolved(request_id: str | int = 71, *, thread: str = "thread-example") -> dict:
    return {
        "method": "serverRequest/resolved",
        "params": {"threadId": thread, "requestId": request_id},
    }


def started_response(request_id: int = 1, turn: str = "turn-example") -> dict:
    return {"id": request_id, "result": {"turn": {"id": turn}}}


def completed(turn: str = "turn-example") -> dict:
    return {
        "method": "turn/completed",
        "params": {"threadId": "thread-example", "turn": {"id": turn, "status": "completed"}},
    }


def tool(
    *, thread: str = "thread-example", turn: str = "turn-example", status: str = "inProgress"
) -> dict:
    return {
        "method": "item/started" if status == "inProgress" else "item/completed",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "item": {
                "id": "tool-example",
                "type": "commandExecution",
                "status": status,
                "command": "private-command",
                "aggregatedOutput": "private-output",
            },
        },
    }


class CodexActivityClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, client: CodexAppServerClient) -> str:
        return client.start_turn(
            thread_id="thread-example",
            cwd=self.root,
            text="Work",
            model="example-model",
            effort="high",
        )

    def client(
        self, messages: list[dict], *, policy: str = "on-request"
    ) -> tuple[CodexAppServerClient, Transport, list[CodexActivityEvent]]:
        transport = Transport(messages)
        client = CodexAppServerClient(transport, initialized=True, approval_policy=policy)
        events: list[CodexActivityEvent] = []
        client.on_activity = events.append
        return client, transport, events

    def test_early_approval_is_payload_free_and_waits_for_accepted_scope(self) -> None:
        client, transport, events = self.client([approval(), started_response(), completed()])
        turn_id = self.start(client)
        self.assertEqual(events, [])
        self.assertNotIn("private", repr(client._pending_activity))
        self.assertNotIn("secret", repr(client._pending_activity))
        client.wait_for_turn(turn_id)
        self.assertEqual([event.kind for event in events], ["approval_requested"])
        self.assertEqual(events[0].turn_id, turn_id)
        self.assertEqual([message["method"] for message in transport.sent], ["turn/start"])
        self.assertNotIn("command", asdict(events[0]))

    def test_early_resolved_maps_only_matching_typed_request_and_thread(self) -> None:
        client, _, events = self.client(
            [
                approval(),
                resolved("71"),
                resolved(thread="other-thread"),
                resolved(),
                started_response(),
                completed(),
            ]
        )
        turn_id = self.start(client)
        self.assertEqual(events, [])
        client.wait_for_turn(turn_id)
        self.assertEqual(
            [event.kind for event in events], ["approval_requested", "approval_resolved"]
        )
        self.assertEqual(events[1].turn_id, turn_id)
        self.assertEqual(type(events[1].request_id), int)

    def test_missing_or_contradictory_resolution_scope_is_ignored(self) -> None:
        wrong_turn = resolved()
        wrong_turn["params"]["turnId"] = "other-turn"
        missing_thread = resolved()
        del missing_thread["params"]["threadId"]
        client, _, events = self.client(
            [
                started_response(),
                approval(),
                wrong_turn,
                missing_thread,
                resolved(99),
                resolved(),
                completed(),
            ]
        )
        client.wait_for_turn(self.start(client))
        self.assertEqual(
            [event.kind for event in events], ["approval_requested", "approval_resolved"]
        )

    def test_other_thread_or_turn_never_enters_callback(self) -> None:
        client, _, events = self.client(
            [
                approval(thread="other-thread"),
                approval(turn="other-turn"),
                started_response(),
                tool(thread="other-thread"),
                tool(turn="other-turn"),
                tool(),
                completed(),
            ]
        )
        client.wait_for_turn(self.start(client))
        self.assertEqual([event.kind for event in events], ["tool_started"])

    def test_completed_visible_activity_and_tool_output_exclude_payloads(self) -> None:
        visible = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-example",
                "turnId": "turn-example",
                "item": {
                    "id": "message-example",
                    "type": "agentMessage",
                    "text": "Visible result",
                    "phase": "final_answer",
                },
            },
        }
        output = {
            "method": "item/commandExecution/outputDelta",
            "params": {
                "threadId": "thread-example",
                "turnId": "turn-example",
                "itemId": "tool-example",
                "delta": "private-output",
            },
        }
        client, _, events = self.client(
            [started_response(), tool(), output, tool(status="completed"), visible, completed()]
        )
        result = client.wait_for_turn(self.start(client))
        self.assertEqual(result.text, "Visible result")
        self.assertEqual(
            [event.kind for event in events],
            ["tool_started", "tool_output", "tool_completed", "visible_message_completed"],
        )
        self.assertNotIn("private", repr(events))
        self.assertNotIn("Visible result", repr(events))

    def test_duplicate_approval_and_resolution_emit_once(self) -> None:
        client, _, events = self.client(
            [
                approval(),
                approval(),
                resolved(),
                resolved(),
                started_response(),
                approval(),
                resolved(),
                completed(),
            ]
        )
        client.wait_for_turn(self.start(client))
        self.assertEqual(
            [event.kind for event in events], ["approval_requested", "approval_resolved"]
        )

    def test_request_identifier_collision_with_other_turn_fails_closed(self) -> None:
        client, transport, events = self.client(
            [approval(), approval(turn="other-turn"), started_response()]
        )
        with self.assertRaises(RpcError):
            self.start(client)
        self.assertEqual(events, [])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(list(client._pending_activity), [])

    def test_pending_activity_overflow_fails_before_callback_and_clears(self) -> None:
        messages = [approval(index) for index in range(200)]
        client, transport, events = self.client(messages)
        with self.assertRaises(RpcError):
            self.start(client)
        self.assertEqual(events, [])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(list(client._pending_activity), [])

    def test_stdio_declines_before_activity_callback_and_never_allows(self) -> None:
        client, transport, events = self.client(
            [approval(), started_response(), completed()], policy="never"
        )
        client.wait_for_turn(self.start(client))
        self.assertEqual(transport.sent[1], {"id": 71, "result": {"decision": "decline"}})
        self.assertEqual(events, [])

    def test_callback_failure_after_stdio_decline_has_no_second_invocation(self) -> None:
        client, transport, _ = self.client(
            [started_response(), approval(), tool(), completed()], policy="never"
        )

        def fail(event: CodexActivityEvent) -> None:
            raise RuntimeError("activity persistence failed")

        client.on_activity = fail
        with self.assertRaises(CodexTurnError):
            client.wait_for_turn(self.start(client))
        self.assertEqual(transport.sent[1], {"id": 71, "result": {"decision": "decline"}})
        self.assertEqual(
            sum(message.get("method") == "turn/start" for message in transport.sent), 1
        )
        self.assertEqual(list(client._pending_activity), [])

    def test_wait_scope_cannot_substitute_another_turn(self) -> None:
        client, _, events = self.client([approval(), started_response(), completed("other-turn")])
        self.start(client)
        client.wait_for_turn("other-turn")
        self.assertEqual(events, [])
        self.assertEqual(list(client._pending_activity), [])

    def test_start_failure_drops_early_activity_and_reused_client_is_clean(self) -> None:
        client, transport, events = self.client(
            [
                approval(),
                {"id": 1, "error": {"message": "rejected"}},
                started_response(2, "later-turn"),
                completed("later-turn"),
            ]
        )
        with self.assertRaises(RpcError):
            self.start(client)
        self.assertEqual(list(client._pending_activity), [])
        client.wait_for_turn(self.start(client))
        self.assertEqual(events, [])
        self.assertEqual(
            sum(message.get("method") == "turn/start" for message in transport.sent), 2
        )

    def test_new_start_drops_previous_accepted_but_unobserved_activity(self) -> None:
        client, _, events = self.client(
            [
                approval(),
                started_response(),
                started_response(2, "later-turn"),
                completed("later-turn"),
            ]
        )
        self.start(client)
        client.wait_for_turn(self.start(client))
        self.assertEqual(events, [])

    def test_direct_wait_without_known_thread_emits_no_guessed_activity(self) -> None:
        client, _, events = self.client([tool(), approval(), completed()])
        client.wait_for_turn("turn-example")
        self.assertEqual(events, [])

    def test_failed_thread_resume_clears_previous_accepted_activity(self) -> None:
        client, _, events = self.client(
            [approval(), started_response(), {"id": 2, "error": {"message": "cannot resume"}}]
        )
        self.start(client)
        with self.assertRaises(RpcError):
            client.resume_thread(thread_id="different-thread", cwd=self.root, model="example-model")
        self.assertEqual(list(client._pending_activity), [])
        self.assertEqual(events, [])

    def test_start_transport_loss_clears_early_request_evidence(self) -> None:
        client, _, events = self.client([approval()])
        with self.assertRaises(EOFError):
            self.start(client)
        self.assertEqual(list(client._pending_activity), [])
        self.assertEqual(client._activity_requests, {})
        self.assertEqual(events, [])

    def test_callback_order_follows_persisted_acceptance(self) -> None:
        client, _, _ = self.client([approval(), started_response(), completed()])
        boundaries: list[str] = []
        client.on_activity = lambda event: boundaries.append(event.kind)
        turn = self.start(client)
        boundaries.append("acceptance-persisted")
        client.wait_for_turn(turn)
        self.assertEqual(boundaries, ["acceptance-persisted", "approval_requested"])

    def test_early_tools_and_approvals_preserve_order_without_duplicate_callbacks(self) -> None:
        client, _, events = self.client(
            [
                tool(),
                approval(),
                resolved(),
                tool(status="completed"),
                started_response(),
                completed(),
            ]
        )
        turn = self.start(client)
        self.assertEqual(events, [])
        client.wait_for_turn(turn)
        self.assertEqual(
            [event.kind for event in events],
            ["tool_started", "approval_requested", "approval_resolved", "tool_completed"],
        )
