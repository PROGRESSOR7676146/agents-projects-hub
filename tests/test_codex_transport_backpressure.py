from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

from hermes_codex_router.codex_appserver import (
    CodexAppServerClient,
    RpcError,
    UnixWebSocketTransport,
)
from hermes_codex_router.codex_inbox import BoundedInbox


class BoundedInboxTests(unittest.TestCase):
    def test_full_fifo_drains_before_terminal_without_an_extra_error_slot(self):
        inbox = BoundedInbox(max_frames=1, max_bytes=8, max_frame_bytes=8)
        self.assertTrue(inbox.try_put({"id": "example-response"}, 8))
        self.assertFalse(inbox.try_put({"id": "example-next"}, 1))
        inbox.finish(EOFError("fictional EOF"))
        self.assertEqual(inbox.get(timeout=0), {"id": "example-response"})
        with self.assertRaises(EOFError):
            inbox.get(timeout=0)

    def test_byte_capacity_and_oversized_frame_fail_without_losing_accepted_data(self):
        inbox = BoundedInbox(max_frames=3, max_bytes=8, max_frame_bytes=8)
        self.assertTrue(inbox.try_put("example-a", 7))
        self.assertFalse(inbox.try_put("example-b", 2))
        with self.assertRaises(RpcError):
            inbox.try_put("example-oversized", 9)
        self.assertEqual(inbox.get(timeout=0), "example-a")


class ScriptedSocket:
    def __init__(self, messages):
        self.messages = messages
        self.pulled = threading.Event()
        self.sent = threading.Event()
        self.full = threading.Event()
        self.sent_values = []
        self.closed = False
        self.send_error = False
        self.stall_send = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def __aiter__(self):
        for message in self.messages:
            if message is None:
                return
            self.pulled.set()
            yield SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                data=message if isinstance(message, str) else json.dumps(message),
            )
        await asyncio.Event().wait()

    async def send_str(self, message):
        self.sent_values.append(json.loads(message))
        self.sent.set()
        if self.send_error:
            raise OSError("fictional send failure")
        if self.stall_send:
            await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class ScriptedSession:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def ws_connect(self, *args, **kwargs):
        return self.websocket


class WebSocketBackpressureTests(unittest.TestCase):
    def transport(self, messages, *, inbox=None):
        temporary = tempfile.TemporaryDirectory(prefix="example-bounded-websocket-")
        self.addCleanup(temporary.cleanup)
        endpoint = Path(temporary.name) / "example.sock"
        endpoint.touch()
        websocket = ScriptedSocket(messages)

        class ObservedInbox(BoundedInbox):
            def try_put(self, value, size):
                result = super().try_put(value, size)
                if not result:
                    websocket.full.set()
                return result

        storage = inbox if inbox is not None else ObservedInbox(max_frames=1)
        factory = patch("hermes_codex_router.codex_transports.BoundedInbox", return_value=storage)
        connector = patch("hermes_codex_router.codex_transports.aiohttp.UnixConnector")
        session = patch(
            "hermes_codex_router.codex_transports.aiohttp.ClientSession",
            return_value=ScriptedSession(websocket),
        )
        connector.start()
        session.start()
        self.addCleanup(connector.stop)
        self.addCleanup(session.stop)
        with factory:
            transport = UnixWebSocketTransport(endpoint, timeout=1, max_pending_frames=1)
        self.addCleanup(transport.close)
        return transport, websocket

    def test_full_inbox_does_not_block_send_and_preserves_rpc_approval_and_final(self):
        messages = [
            {"method": "item/agentMessage/delta", "params": {"threadId": "example-other"}}
            for _ in range(1100)
        ] + [
            {"id": 1, "result": {"turn": {"id": "example-turn"}}},
            {"id": "example-approval", "method": "item/fileChange/requestApproval"},
            {"method": "item/completed", "params": {"turnId": "example-turn"}},
            {"method": "turn/completed", "params": {"turn": {"id": "example-turn"}}},
        ]
        transport, websocket = self.transport(messages)
        self.assertTrue(websocket.full.wait(1))
        transport.send({"id": 1, "method": "turn/start", "params": {}})
        self.assertTrue(websocket.sent.wait(1), "full inbox stranded sender/event loop")
        self.assertEqual([transport.receive(timeout=1) for _ in messages], messages)
        self.assertLessEqual(transport._inbound.pending_frames, 1)

    def test_real_client_filters_foreign_flood_and_keeps_exact_final_and_context(self):
        messages = [
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "example-other", "turnId": "example-other-turn"},
            }
            for _ in range(1100)
        ] + [
            {"id": 1, "result": {"turn": {"id": "example-turn"}}},
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "example-thread",
                    "turnId": "example-turn",
                    "tokenUsage": {"last": {"totalTokens": 25}, "modelContextWindow": 100},
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "example-thread",
                    "turnId": "example-turn",
                    "item": {
                        "id": "example-final",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Example final.",
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "example-thread",
                    "turn": {"id": "example-turn", "status": "completed"},
                },
            },
        ]
        transport, websocket = self.transport(messages)
        client = CodexAppServerClient(transport, initialized=True)
        turn = client.start_turn(
            thread_id="example-thread",
            text="Example input.",
            cwd=transport._socket_path.parent,
            model="example-offline",
            effort="high",
        )
        result = client.wait_for_turn(turn)
        self.assertEqual(turn, "example-turn")
        self.assertEqual(result.text, "Example final.")
        self.assertEqual((result.context_window, result.context_tokens_used), (100, 25))
        self.assertTrue(websocket.sent.wait(1))
        self.assertEqual([message["method"] for message in websocket.sent_values], ["turn/start"])

    def test_capacity_released_between_full_check_and_await_is_not_lost(self):
        drained = []
        capacity_released = threading.Event()

        class RaceInbox(BoundedInbox):
            def try_put(self, value, size):
                result = super().try_put(value, size)
                if not result:
                    drained.append(self.get(timeout=0))
                    capacity_released.set()
                return result

        inbox = RaceInbox(max_frames=1)
        prefill = json.dumps({"method": "example-prefill"})
        self.assertTrue(inbox.try_put(prefill, len(prefill)))
        transport, _ = self.transport([{"method": "example-after-capacity-release"}], inbox=inbox)
        self.assertTrue(capacity_released.wait(1), "producer never reached the forced full race")
        self.assertEqual(transport.receive(timeout=1), {"method": "example-after-capacity-release"})
        self.assertEqual(drained, [prefill])

    def test_close_wakes_full_producer_and_waiting_receiver(self):
        transport, websocket = self.transport([{"method": "example"}] * 3)
        self.assertTrue(websocket.full.wait(1))
        transport.close()
        self.assertFalse(transport._thread.is_alive())
        # Accepted frames remain readable, then the out-of-band close wakes receive.
        while transport._inbound.pending_frames:
            transport.receive(timeout=1)
        with self.assertRaises(RpcError):
            transport.receive(timeout=1)

    def test_eof_drains_every_queued_frame_before_terminal(self):
        transport, websocket = self.transport([{"id": 1}, {"id": 2}, None])
        self.assertTrue(websocket.full.wait(1))
        self.assertEqual(transport.receive(timeout=1), {"id": 1})
        self.assertEqual(transport.receive(timeout=1), {"id": 2})
        with self.assertRaisesRegex(RpcError, "EOFError"):
            transport.receive(timeout=1)
        transport._thread.join(1)
        self.assertFalse(transport._thread.is_alive())

    def test_malformed_frame_keeps_earlier_frame_and_reports_bounded_cause(self):
        transport, websocket = self.transport([{"id": 1}, "{", None])
        self.assertTrue(websocket.full.wait(1))
        self.assertEqual(transport.receive(timeout=1), {"id": 1})
        with self.assertRaisesRegex(RpcError, "emitted malformed JSON"):
            transport.receive(timeout=1)

    def test_sender_failure_while_full_preserves_accepted_frame_and_first_cause(self):
        transport, websocket = self.transport([{"id": 1}, {"id": 2}])
        self.assertTrue(websocket.full.wait(1))
        websocket.send_error = True
        transport.send({"method": "example-request"})
        self.assertTrue(websocket.sent.wait(1))
        transport._thread.join(1)
        self.assertFalse(transport._thread.is_alive())
        self.assertEqual(transport.receive(timeout=1), {"id": 1})
        with self.assertRaisesRegex(RpcError, "OSError"):
            transport.receive(timeout=1)

    def test_close_wakes_consumer_already_waiting_on_empty_inbox(self):
        transport, _ = self.transport([])
        waiting = threading.Event()
        failures = []
        original_wait = transport._inbound._condition.wait

        def wait(timeout):
            waiting.set()
            return original_wait(timeout)

        def receive():
            try:
                transport.receive(timeout=2)
            except RpcError as error:
                failures.append(type(error))

        with patch.object(transport._inbound._condition, "wait", side_effect=wait):
            consumer = threading.Thread(target=receive, daemon=True)
            consumer.start()
            self.assertTrue(waiting.wait(1))
            transport.close()
            consumer.join(1)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(failures, [RpcError])

    def test_outbound_queue_and_frame_bounds_fail_without_an_extra_send(self):
        transport, websocket = self.transport([])
        websocket.stall_send = True
        transport.send({"id": 0})
        self.assertTrue(websocket.sent.wait(1))
        for identifier in range(1, 17):
            transport.send({"id": identifier})
        with self.assertRaisesRegex(RpcError, "outbound buffer"):
            transport.send({"id": 17})
        with self.assertRaisesRegex(RpcError, "outbound frame"):
            transport.send({"text": "x" * (4 * 1024 * 1024)})
        self.assertEqual(len(websocket.sent_values), 1)
        self.assertEqual(transport._outbound.qsize(), 16)

    def test_timeout_does_not_poison_later_delivery_and_interrupts_propagate(self):
        transport, _ = self.transport([])
        with self.assertRaisesRegex(RpcError, "timed out"):
            transport.receive(timeout=0.01)
        frame = json.dumps({"id": "example-later"})
        self.assertTrue(transport._inbound.try_put(frame, len(frame)))
        self.assertEqual(transport.receive(timeout=1), {"id": "example-later"})
        for interruption in (KeyboardInterrupt, SystemExit):
            with patch.object(transport._inbound, "get", side_effect=interruption):
                with self.assertRaises(interruption):
                    transport.receive(timeout=1)
