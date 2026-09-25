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

    def test_paginated_saved_failed_turn_uses_bounded_read_only_turn_page(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "thread": {
                            "id": "fictional-thread",
                            "cwd": str(self.cwd),
                            "historyMode": "paginated",
                        }
                    },
                },
                {
                    "id": 2,
                    "result": {
                        "data": [{"id": "fictional-turn", "status": "failed"}],
                        "nextCursor": None,
                    },
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)

        outcome = client.read_turn_outcome(
            thread_id="fictional-thread", turn_id="fictional-turn", cwd=self.cwd
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            [message["method"] for message in transport.sent],
            ["thread/read", "thread/turns/list"],
        )
        self.assertEqual(transport.sent[0]["params"]["includeTurns"], False)
        self.assertEqual(transport.sent[1]["params"]["itemsView"], "notLoaded")

    def test_paginated_completed_turn_reads_only_exact_visible_items(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "thread": {
                            "id": "fictional-thread",
                            "cwd": str(self.cwd),
                            "historyMode": "paginated",
                        }
                    },
                },
                {
                    "id": 2,
                    "result": {
                        "data": [{"id": "fictional-turn", "status": "completed"}],
                        "nextCursor": None,
                    },
                },
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "turnId": "fictional-turn",
                                "item": {"id": "hidden", "type": "reasoning", "text": "secret"},
                            },
                            {
                                "turnId": "fictional-turn",
                                "item": {"id": "visible", "type": "agentMessage", "text": "First"},
                            },
                        ],
                        "nextCursor": "next",
                    },
                },
                {
                    "id": 4,
                    "result": {
                        "data": [
                            {
                                "turnId": "fictional-turn",
                                "item": {"id": "visible", "type": "agentMessage", "text": "First"},
                            },
                            {
                                "turnId": "fictional-turn",
                                "item": {"id": "final", "type": "agentMessage", "text": "Done"},
                            },
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)

        outcome = client.read_turn_outcome(
            thread_id="fictional-thread", turn_id="fictional-turn", cwd=self.cwd
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.result.text if outcome.result else None, "First\n\nDone")
        self.assertEqual(
            [message["method"] for message in transport.sent],
            ["thread/read", "thread/turns/list", "thread/items/list", "thread/items/list"],
        )
        self.assertEqual(transport.sent[-1]["params"]["cursor"], "next")

    def test_paginated_turn_search_returns_unknown_without_replay(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "thread": {
                            "id": "fictional-thread",
                            "cwd": str(self.cwd),
                            "historyMode": "paginated",
                        }
                    },
                },
                {"id": 2, "result": {"data": [], "nextCursor": None}},
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)

        outcome = client.read_turn_outcome(
            thread_id="fictional-thread", turn_id="missing-turn", cwd=self.cwd
        )

        self.assertEqual(outcome.status, "unknown")
        self.assertEqual(
            [message["method"] for message in transport.sent], ["thread/read", "thread/turns/list"]
        )

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
            developer_instructions="Telegram contract",
        )

        self.assertEqual(thread.thread_id, "thread-123")
        self.assertEqual(transport.sent[0]["method"], "initialize")
        self.assertEqual(transport.sent[1], {"method": "initialized", "params": {}})
        request = transport.sent[2]
        self.assertEqual(request["method"], "thread/start")
        self.assertEqual(request["params"]["cwd"], str(self.cwd))
        self.assertEqual(request["params"]["sandbox"], "workspace-write")
        self.assertEqual(request["params"]["approvalPolicy"], "on-request")
        self.assertEqual(request["params"]["developerInstructions"], "Telegram contract")
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

    def test_turn_start_passes_verified_project_image_as_native_local_image(self) -> None:
        image = self.cwd / ".hub" / "incoming" / "job-1" / "material-01.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\x89PNG\r\n\x1a\nfictional")
        transport = FakeTransport([{"id": 1, "result": {"turn": {"id": "turn-image"}}}])
        client = CodexAppServerClient(transport, initialized=True)

        client.start_turn(
            thread_id="thread-123",
            cwd=self.cwd,
            text="inspect the image",
            model="gpt-5.6-sol",
            effort="high",
            local_image_paths=(image,),
        )

        self.assertEqual(
            transport.sent[0]["params"]["input"],
            [
                {"type": "text", "text": "inspect the image"},
                {"type": "localImage", "path": str(image)},
            ],
        )

    def test_turn_start_rejects_local_image_outside_project(self) -> None:
        image = Path(self.tempdir.name) / "outside.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfictional")
        client = CodexAppServerClient(FakeTransport([]), initialized=True)

        with self.assertRaisesRegex(RpcError, "outside the execution root"):
            client.start_turn(
                thread_id="thread-123",
                cwd=self.cwd,
                text="inspect",
                model="gpt-5.6-sol",
                effort="high",
                local_image_paths=(image,),
            )

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

    def test_connectable_threads_include_cli_and_vscode_sessions(self) -> None:
        def thread(thread_id: str, source: str) -> dict[str, object]:
            return {
                "id": thread_id,
                "cwd": str(self.cwd),
                "status": {"type": "notLoaded", "activeFlags": []},
                "updatedAt": 1_800_000_000,
                "ephemeral": False,
                "modelProvider": "openai",
                "source": source,
                "name": f"Saved {source}",
            }

        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "data": [
                            thread("example-cli-thread", "cli"),
                            thread("example-vscode-thread", "vscode"),
                            thread("example-exec-thread", "exec"),
                        ]
                    },
                }
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)

        sessions = client.list_connectable_threads(root=self.cwd)

        self.assertEqual(
            [item.thread_id for item in sessions],
            [
                "example-cli-thread",
                "example-vscode-thread",
            ],
        )
        self.assertEqual(
            transport.sent[0]["params"]["sourceKinds"],
            ["cli", "vscode"],
        )

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
            developer_instructions="Telegram reminder",
        )
        self.assertEqual(thread.thread_id, "thread-123")
        params = transport.sent[0]["params"]
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["developerInstructions"], "Telegram reminder")

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
                            "primary": {
                                "usedPercent": 35,
                                "resetsAt": 1770000000,
                                "windowDurationMins": 300,
                            },
                            "secondary": {
                                "usedPercent": 52,
                                "resetsAt": 1770500000,
                                "windowDurationMins": 10080,
                            },
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
        self.assertEqual(limits.primary.duration_minutes, 300)
        self.assertEqual(limits.secondary.duration_minutes, 10080)

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
        self.assertEqual(result.context_tokens_used, 1000)
        self.assertNotIn("reasoning", result.text)
        self.assertEqual(transport.receive_timeouts, [3600.0] * 4)

    def test_wait_for_turn_uses_latest_post_compaction_context_usage(self) -> None:
        transport = FakeTransport(
            [
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-123",
                        "turnId": "turn-9",
                        "tokenUsage": {
                            "modelContextWindow": 100000,
                            "last": {"totalTokens": 80000},
                            "total": {"totalTokens": 180000},
                        },
                    },
                },
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-123",
                        "turnId": "turn-9",
                        "tokenUsage": {
                            "modelContextWindow": 100000,
                            "last": {"totalTokens": 20000},
                            "total": {"totalTokens": 200000},
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-123", "turn": {"id": "turn-9"}},
                },
            ]
        )

        result = CodexAppServerClient(transport, initialized=True).wait_for_turn("turn-9")

        self.assertEqual(result.context_window, 100000)
        self.assertEqual(result.context_tokens_used, 20000)

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

    def test_stdio_fallback_declines_unexpected_approval_instead_of_deadlocking(self) -> None:
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
        client = CodexAppServerClient(transport, initialized=True, approval_policy="never")

        client.wait_for_turn("turn-9")

        self.assertEqual(
            transport.sent,
            [{"id": 81, "result": {"decision": "decline"}}],
        )

    def test_never_policy_is_pinned_with_workspace_sandbox(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "thread": {"id": "thread-123"},
                        "cwd": str(self.cwd),
                        "model": "gpt-5.6-sol",
                        "modelProvider": "openai",
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                    },
                }
            ]
        )
        client = CodexAppServerClient(transport, initialized=True, approval_policy="never")

        client.start_thread(cwd=self.cwd, model="gpt-5.6-sol", project_id="alpha")

        params = transport.sent[0]["params"]
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertNotIn("approvalsReviewer", params)

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
