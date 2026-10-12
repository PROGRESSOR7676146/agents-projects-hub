from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError
from tests.test_codex_appserver import FakeTransport


def visible(text: str, *, thread: str = "example-thread", turn: str = "example-turn") -> dict:
    return {
        "method": "item/completed",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "item": {"id": text.replace(" ", "-"), "type": "agentMessage", "text": text},
        },
    }


def completed(*, thread: str = "example-thread", turn: str = "example-turn") -> dict:
    return {
        "method": "turn/completed",
        "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}},
    }


class CodexNotificationIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def start(self, client: CodexAppServerClient) -> str:
        return client.start_turn(
            thread_id="example-thread",
            cwd=self.root,
            text="Example request",
            model="example-model",
            effort="high",
        )

    def test_start_and_resume_ignore_unconsumed_startup_notifications(self) -> None:
        noise = [
            {"method": method, "params": {"threadId": "other-thread", "delta": "private"}}
            for _ in range(600)
            for method in ("thread/status/changed", "item/reasoning/textDelta")
        ]
        for resume in (False, True):
            with self.subTest(resume=resume):
                transport = FakeTransport(
                    noise
                    + [
                        {
                            "id": 1,
                            "result": {
                                "thread": {"id": "example-thread"},
                                "cwd": str(self.root),
                                "approvalPolicy": "on-request",
                                "sandbox": {"type": "workspaceWrite"},
                            },
                        }
                    ]
                )
                client = CodexAppServerClient(transport, initialized=True)
                if resume:
                    thread = client.resume_thread(
                        thread_id="example-thread", cwd=self.root, model="example-model"
                    )
                else:
                    thread = client.start_thread(
                        cwd=self.root, model="example-model", project_id="example-project"
                    )
                self.assertEqual(thread.thread_id, "example-thread")
                self.assertEqual(list(client.notifications), [])
                self.assertEqual(len(transport.sent), 1)

    def test_current_turn_delta_flood_does_not_displace_early_final_result(self) -> None:
        noise = [
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "example-thread",
                    "turnId": "example-turn",
                    "delta": "private",
                },
            }
            for _ in range(1500)
        ]
        transport = FakeTransport(
            noise
            + [
                visible("Saved answer"),
                completed(),
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        events = []
        client.on_activity = events.append
        turn = self.start(client)
        self.assertEqual(len(client.notifications), 2)
        self.assertEqual(len(client._activity_observed_notifications), 2)
        self.assertEqual(client.wait_for_turn(turn).text, "Saved answer")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "visible_message_completed")
        self.assertEqual([message["method"] for message in transport.sent], ["turn/start"])

    def test_other_thread_flood_does_not_enter_current_result_or_buffer(self) -> None:
        noise = [visible("Foreign result", thread="other-thread") for _ in range(1500)]
        transport = FakeTransport(
            noise
            + [
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                visible("Wrong result", thread="other-thread"),
                completed(thread="other-thread"),
                visible("Own result"),
                completed(),
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        turn = self.start(client)
        self.assertEqual(list(client.notifications), [])
        self.assertEqual(client.wait_for_turn(turn).text, "Own result")

    def test_stale_turn_does_not_fill_buffer_during_followup_rpc(self) -> None:
        transport = FakeTransport(
            [
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                *[visible("Stale result", turn="old-turn") for _ in range(1500)],
                {"id": 2, "result": {"turnId": "example-turn"}},
                visible("Own result"),
                completed(),
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        turn = self.start(client)
        client.steer_turn(
            thread_id="example-thread",
            turn_id=turn,
            text="Example followup",
            client_user_message_id="example-message",
        )
        self.assertEqual(list(client.notifications), [])
        self.assertEqual(client.wait_for_turn(turn).text, "Own result")

    def test_useful_current_turn_overflow_still_fails_closed(self) -> None:
        transport = FakeTransport([visible(str(i)) for i in range(1025)])
        client = CodexAppServerClient(transport, initialized=True)
        with self.assertRaisesRegex(RpcError, "notification buffer"):
            self.start(client)
        self.assertEqual(len(client.notifications), 1024)
        self.assertEqual([message["method"] for message in transport.sent], ["turn/start"])

    def test_passive_metadata_does_not_retain_visible_text_from_idle_thread(self) -> None:
        transport = FakeTransport(
            [
                *[visible("Private idle text") for _ in range(1500)],
                {"id": 1, "result": {"data": []}},
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        self.assertEqual(client.list_models(), ())
        self.assertEqual(list(client.notifications), [])
        self.assertEqual([message["method"] for message in transport.sent], ["model/list"])
