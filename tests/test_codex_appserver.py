from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path

from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError


class FakeTransport:
    def __init__(self, incoming: list[dict]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[dict] = []
        self.receive_timeouts: list[float | None] = []

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def receive(self, *, timeout: float | None = None) -> dict:
        self.receive_timeouts.append(timeout)
        if not self.incoming:
            raise EOFError("fake transport exhausted")
        return self.incoming.popleft()

    def close(self) -> None:
        pass


class CodexAppServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tempdir.name) / "Example Project Alpha"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_initialize_and_thread_start_pin_safe_project_policy(self) -> None:
        transport = FakeTransport(
            [
                {"id": 1, "result": {"userAgent": "codex-test"}},
                {
                    "id": 2,
                    "result": {
                        "thread": {"id": "thread-123"},
                        "cwd": str(self.cwd),
                        "model": "gpt-5.6-sol",
                        "modelProvider": "openai",
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "user",
                        "sandbox": {
                            "type": "workspaceWrite",
                            "writableRoots": [str(self.cwd)],
                            "networkAccess": False,
                        },
                    },
                },
            ]
        )
        client = CodexAppServerClient(transport)
        client.initialize()
        thread = client.start_thread(
            cwd=self.cwd,
            model="gpt-5.6-sol",
            project_id="alpha",
        )

        self.assertEqual(thread.thread_id, "thread-123")
        self.assertEqual(transport.sent[0]["method"], "initialize")
        self.assertEqual(transport.sent[1], {"method": "initialized", "params": {}})
        request = transport.sent[2]
        self.assertEqual(request["method"], "thread/start")
        self.assertEqual(request["params"]["cwd"], str(self.cwd))
        self.assertEqual(request["params"]["sandbox"], "workspace-write")
        self.assertEqual(request["params"]["approvalPolicy"], "on-request")
        self.assertNotIn("projectId", request["params"])
        self.assertNotIn("danger-full-access", str(request))

    def test_turn_start_passes_model_effort_and_text_as_json_not_shell(self) -> None:
        transport = FakeTransport([{"id": 1, "result": {"turn": {"id": "turn-9"}}}])
        client = CodexAppServerClient(transport, initialized=True)
        turn_id = client.start_turn(
            thread_id="thread-123",
            cwd=self.cwd,
            text="inspect; touch /tmp/no",
            model="gpt-5.6-sol",
            effort="high",
        )

        self.assertEqual(turn_id, "turn-9")
        params = transport.sent[0]["params"]
        self.assertEqual(params["input"], [{"type": "text", "text": "inspect; touch /tmp/no"}])
        self.assertEqual(params["effort"], "high")

    def test_turn_steer_and_interrupt_use_active_turn_preconditions(self) -> None:
        transport = FakeTransport(
            [
                {"id": 1, "result": {"turnId": "turn-9"}},
                {"id": 2, "result": {}},
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)

        returned = client.steer_turn(
            thread_id="thread-123",
            turn_id="turn-9",
            text="new direction",
            client_user_message_id="telegram-message-2",
        )
        client.interrupt_turn(thread_id="thread-123", turn_id="turn-9")

        self.assertEqual(returned, "turn-9")
        self.assertEqual(transport.sent[0]["method"], "turn/steer")
        self.assertEqual(transport.sent[0]["params"]["expectedTurnId"], "turn-9")
        self.assertEqual(
            transport.sent[0]["params"]["input"],
            [{"type": "text", "text": "new direction"}],
        )
        self.assertEqual(transport.sent[1]["method"], "turn/interrupt")

    def test_resume_thread_reasserts_safe_policy(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "thread": {"id": "thread-123"},
                        "cwd": str(self.cwd),
                        "model": "gpt-5.6-sol",
                        "modelProvider": "openai",
                        "approvalPolicy": "on-request",
                        "sandbox": {"type": "workspaceWrite", "networkAccess": False},
                    },
                }
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        thread = client.resume_thread(
            thread_id="thread-123",
            cwd=self.cwd,
            model="gpt-5.6-sol",
        )
        self.assertEqual(thread.thread_id, "thread-123")
        params = transport.sent[0]["params"]
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandbox"], "workspace-write")

    def test_rpc_error_is_not_treated_as_result(self) -> None:
        transport = FakeTransport([{"id": 1, "error": {"code": -32602, "message": "bad"}}])
        client = CodexAppServerClient(transport, initialized=True)
        with self.assertRaisesRegex(RpcError, "bad"):
            client.list_models()

    def test_rate_limit_snapshot_exposes_remaining_percent_and_reset(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "rateLimits": {
                            "primary": {"usedPercent": 35, "resetsAt": 1770000000},
                            "secondary": {"usedPercent": 52, "resetsAt": 1770500000},
                        }
                    },
                }
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        limits = client.read_rate_limits()
        self.assertIsNotNone(limits.primary)
        self.assertIsNotNone(limits.secondary)
        assert limits.primary is not None
        assert limits.secondary is not None
        self.assertEqual(limits.primary.remaining_percent, 65)
        self.assertEqual(limits.secondary.remaining_percent, 48)
        self.assertEqual(limits.primary.resets_at, 1770000000)

    def test_wait_for_turn_returns_only_completed_agent_message_and_usage(self) -> None:
        transport = FakeTransport(
            [
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-123",
                        "turnId": "turn-9",
                        "tokenUsage": {
                            "modelContextWindow": 100000,
                            "last": {"totalTokens": 1000},
                            "total": {"totalTokens": 25000},
                        },
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-123",
                        "turnId": "turn-9",
                        "item": {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "summary": ["must never be forwarded"],
                        },
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-123",
                        "turnId": "turn-9",
                        "item": {"id": "answer-1", "type": "agentMessage", "text": "Done"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-123", "turn": {"id": "turn-9"}},
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        result = client.wait_for_turn("turn-9")
        self.assertEqual(result.text, "Done")
        self.assertEqual(result.context_window, 100000)
        self.assertEqual(result.context_tokens_used, 25000)
        self.assertNotIn("reasoning", result.text)
        self.assertEqual(transport.receive_timeouts, [3600.0] * 4)

    def test_server_approval_request_is_left_for_tlive_and_never_auto_allowed(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 81,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "thread-123", "turnId": "turn-9"},
                },
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-123", "turn": {"id": "turn-9"}},
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        client.wait_for_turn("turn-9")
        self.assertEqual(transport.sent, [])

    def test_wait_for_turn_uses_nested_terminal_error_message(self) -> None:
        transport = FakeTransport(
            [
                {
                    "method": "error",
                    "params": {
                        "threadId": "thread-123",
                        "turnId": "turn-9",
                        "willRetry": False,
                        "error": {"message": "usage limit reached"},
                    },
                }
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        with self.assertRaisesRegex(RpcError, "usage limit reached"):
            client.wait_for_turn("turn-9")

    def test_wait_for_turn_ignores_retrying_error_then_completes(self) -> None:
        transport = FakeTransport(
            [
                {
                    "method": "error",
                    "params": {
                        "threadId": "thread-123",
                        "turnId": "turn-9",
                        "willRetry": True,
                        "error": {"message": "temporary transport failure"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-123",
                        "turn": {"id": "turn-9", "status": "completed"},
                    },
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        self.assertEqual(client.wait_for_turn("turn-9").text, "")


if __name__ == "__main__":
    unittest.main()
