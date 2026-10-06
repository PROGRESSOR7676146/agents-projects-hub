from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.codex_appserver import (
    CodexAppServerClient,
    CodexTurnError,
    LimitWindow,
    RateLimits,
    RpcError,
)
from tests.test_codex_appserver import FakeTransport


class CompletedConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def completed_client(
        self, *, status="completed", thread="example-thread", enabled=True, rate_limits=None
    ):
        transport = FakeTransport(
            [
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                {"method": "account/rateLimits/updated", "params": {"rateLimits": rate_limits}},
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread,
                        "turn": {"id": "example-turn", "status": status},
                    },
                },
            ]
        )
        client = CodexAppServerClient(
            transport, initialized=True, retire_completed_connection=enabled
        )
        client.start_turn(
            thread_id="example-thread",
            cwd=self.root,
            text="Example task",
            model="example",
            effort="high",
        )
        client.wait_for_turn("example-turn")
        return client, transport

    def test_failed_terminal_event_or_completion_callback_never_creates_retirement_proof(self):
        for failure in ("failed", "interrupted", "callback", "wrong-thread"):
            with self.subTest(failure=failure):
                transport = FakeTransport(
                    [
                        {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": (
                                    "other-thread"
                                    if failure == "wrong-thread"
                                    else "example-thread"
                                ),
                                "turn": {
                                    "id": "example-turn",
                                    "status": failure if failure != "callback" else "completed",
                                },
                            },
                        },
                    ]
                )
                client = CodexAppServerClient(
                    transport, initialized=True, retire_completed_connection=True
                )
                client.start_turn(
                    thread_id="example-thread",
                    cwd=self.root,
                    text="Example",
                    model="example",
                    effort="high",
                )
                if failure == "callback":
                    client.on_completed = lambda _: (_ for _ in ()).throw(
                        RuntimeError("Example persistence failure")
                    )
                with self.assertRaises((CodexTurnError, RuntimeError)):
                    client.wait_for_turn("example-turn")
                self.assertFalse(
                    client.consume_completed_connection(
                        thread_id="example-thread", turn_id="example-turn"
                    )
                )

    def test_only_exact_explicit_completed_socket_proof_can_be_consumed_once(self) -> None:
        client, transport = self.completed_client()
        self.assertTrue(
            client.consume_completed_connection(thread_id="example-thread", turn_id="example-turn")
        )
        self.assertFalse(
            client.consume_completed_connection(thread_id="example-thread", turn_id="example-turn")
        )
        self.assertEqual([message["method"] for message in transport.sent], ["turn/start"])

    def test_unknown_status_or_unenabled_transport_never_authorizes_retirement(self) -> None:
        for status, enabled in ((None, True), ("unknown", True), ("completed", False)):
            with self.subTest(status=status, enabled=enabled):
                client, _ = self.completed_client(status=status, enabled=enabled)
                self.assertFalse(
                    client.consume_completed_connection(
                        thread_id="example-thread", turn_id="example-turn"
                    )
                )

    def test_wrong_cleanup_identity_cannot_use_another_completed_turn(self) -> None:
        client, _ = self.completed_client()
        self.assertFalse(
            client.consume_completed_connection(thread_id="other-thread", turn_id="example-turn")
        )
        self.assertFalse(
            client.consume_completed_connection(thread_id="example-thread", turn_id="example-turn")
        )

    def test_mismatched_wait_keeps_collection_compatibility_but_cannot_retire_active_turn(
        self,
    ) -> None:
        transport = FakeTransport(
            [
                {"id": 1, "result": {"turn": {"id": "example-active-turn"}}},
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "example-thread",
                        "turn": {"id": "example-other-turn", "status": "completed"},
                    },
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True, retire_completed_connection=True)
        client.start_turn(
            thread_id="example-thread",
            cwd=self.root,
            text="Example",
            model="example",
            effort="high",
        )
        client.wait_for_turn("example-other-turn")
        self.assertFalse(
            client.consume_completed_connection(
                thread_id="example-thread", turn_id="example-other-turn"
            )
        )

    def test_new_preparation_invalidates_old_completion_before_any_rpc(self) -> None:
        for method in ("start_thread", "resume_thread", "start_turn"):
            with self.subTest(method=method):
                client, _ = self.completed_client()
                with self.assertRaises((RpcError, FileNotFoundError)):
                    if method == "start_thread":
                        client.start_thread(
                            cwd=self.root / "missing", model="example", project_id="example"
                        )
                    elif method == "resume_thread":
                        client.resume_thread(
                            thread_id="example-thread", cwd=self.root / "missing", model="example"
                        )
                    else:
                        client.start_turn(
                            thread_id="example-thread",
                            cwd=self.root / "missing",
                            text="Example",
                            model="example",
                            effort="high",
                        )
                self.assertFalse(
                    client.consume_completed_connection(
                        thread_id="example-thread", turn_id="example-turn"
                    )
                )

    def test_post_completion_limit_read_has_total_deadline_and_one_use_fallback(self) -> None:
        snapshot = {
            "limitId": "codex",
            "primary": {"usedPercent": 25, "resetsAt": 101, "windowDurationMins": 300},
            "secondary": {"usedPercent": 70, "resetsAt": 202, "windowDurationMins": 10080},
        }
        expected = RateLimits(LimitWindow(75, 101, 300), LimitWindow(30, 202, 10080))
        client, transport = self.completed_client(rate_limits=snapshot)
        transport.incoming.extend(
            [{"method": "example/foreign", "params": {"threadId": "other"}}] * 30
        )
        moments = iter(range(100))
        with patch(
            "hermes_codex_router.codex_appserver.time.monotonic", side_effect=lambda: next(moments)
        ):
            self.assertEqual(client.read_rate_limits(deadline=4), expected)
            with self.assertRaises(RpcError):
                client.read_rate_limits(deadline=4)
        self.assertGreater(len(transport.incoming), 20)
        client, transport = self.completed_client(rate_limits=snapshot)
        with patch.object(transport, "receive", side_effect=TimeoutError("Example timeout")):
            self.assertEqual(client.read_rate_limits(), expected)
            with self.assertRaises(TimeoutError):
                client.read_rate_limits()
