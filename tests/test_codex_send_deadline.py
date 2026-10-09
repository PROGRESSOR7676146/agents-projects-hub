"""Original proof freshness bounds local writes, independently of RPC replies."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import codex_appserver, codex_transports
from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError
from hermes_codex_router.codex_control_connection import ControlSendPath
from tests import test_codex_transport_backpressure as backpressure
from tests.test_codex_appserver import FakeTransport


class SendDeadlineTests(unittest.TestCase):
    transport = backpressure.WebSocketBackpressureTests.transport

    def setUp(self):
        self.now = 0.0
        clock = SimpleNamespace(monotonic=lambda: self.now)
        self.enterContext(patch.object(codex_transports, "time", clock))
        self.enterContext(patch.object(codex_appserver, "time", clock))

    def block_writer(self, transport, websocket):
        entered = threading.Event()
        gate = asyncio.Event()

        async def send(message):
            websocket.sent_values.append(json.loads(message))
            entered.set()
            await gate.wait()

        websocket.send_str = send
        transport.send({"method": "example-earlier", "id": 0})
        self.assertTrue(entered.wait(1))
        return lambda: transport._wake_event(gate)

    def test_expired_queued_interrupt_is_discarded_and_wakes_rpc_waiter(self):
        transport, websocket = self.transport([])
        release = self.block_writer(transport, websocket)
        admitted = threading.Event()
        original = transport.send_before

        def send(message, *, deadline):
            original(message, deadline=cast(Any, deadline))
            admitted.set()

        errors = []

        def interrupt():
            try:
                CodexAppServerClient(transport, initialized=True).interrupt_turn(
                    thread_id="example-thread",
                    turn_id="example-turn",
                    deadline=10,
                    send_start_deadline=5,
                )
            except RpcError as error:
                errors.append(error)

        with patch.object(transport, "send_before", side_effect=send):
            path = ControlSendPath(transport.close)
            with path.sending_scope() as permitted:
                self.assertTrue(permitted)
                worker = threading.Thread(target=interrupt, daemon=True)
                worker.start()
                self.assertTrue(admitted.wait(1))
                path.request_close()
                self.assertFalse(transport._closed)
                self.now = 5.001
                release()
                worker.join(1)
            self.assertTrue(path.closed)
        self.assertFalse(worker.is_alive(), "queue expiry stranded RPC waiter")
        self.assertEqual(len(errors), 1)
        self.assertIn("send-start deadline", str(errors[0]))
        self.assertEqual([m["method"] for m in websocket.sent_values], ["example-earlier"])
        transport._thread.join(1)
        self.assertFalse(transport._thread.is_alive())

    def test_close_while_interrupt_queued_never_drains_it(self):
        transport, websocket = self.transport([])
        release = self.block_writer(transport, websocket)
        transport.send_before({"method": "turn/interrupt", "id": 1}, deadline=5)
        transport.close()
        release()
        self.assertFalse(transport._thread.is_alive())
        self.assertEqual([m["id"] for m in websocket.sent_values], [0])
        with self.assertRaises(RpcError):
            transport.receive(timeout=1)
        with self.assertRaises(RpcError):
            transport.send_before({"id": 2}, deadline=5)

    def test_expired_or_invalid_admission_never_enqueues(self):
        transport, websocket = self.transport([])
        self.now = 1
        for deadline in (0, -1, float("nan"), float("inf"), True, "5"):
            with self.subTest(deadline=cast(Any, deadline)), self.assertRaises(RpcError):
                transport.send_before({"id": 1}, deadline=cast(Any, deadline))
        self.assertEqual(transport._outbound.qsize(), 0)
        self.assertEqual(websocket.sent_values, [])

    def test_requested_deadline_cannot_fallback_to_unsupported_send(self):
        transport = FakeTransport([])
        with self.assertRaisesRegex(RpcError, "send-start deadline"):
            CodexAppServerClient(transport, initialized=True).interrupt_turn(
                thread_id="example-thread",
                turn_id="example-turn",
                deadline=10,
                send_start_deadline=5,
            )
        self.assertEqual(transport.sent, [])

    def test_invalid_requested_deadlines_never_invoke_send_capability(self):
        for deadline in (float("nan"), float("inf"), True, "5", -1):
            with self.subTest(deadline=cast(Any, deadline)):
                transport = FakeTransport([])
                setattr(
                    transport,
                    "send_before",
                    lambda *args, **kwargs: self.fail("invalid deadline sent"),
                )
                with self.assertRaises(RpcError):
                    CodexAppServerClient(transport, initialized=True).interrupt_turn(
                        thread_id="example-thread",
                        turn_id="example-turn",
                        deadline=10,
                        send_start_deadline=cast(Any, deadline),
                    )
                self.assertEqual(transport.sent, [])

    def test_full_deadline_aware_queue_refuses_without_extra_frame(self):
        transport, websocket = self.transport([])
        self.block_writer(transport, websocket)
        for identifier in range(1, 17):
            transport.send_before({"id": identifier}, deadline=5)
        with self.assertRaisesRegex(RpcError, "outbound buffer"):
            transport.send_before({"id": 17}, deadline=5)
        self.assertEqual(transport._outbound.qsize(), 16)
        self.assertEqual([m["id"] for m in websocket.sent_values], [0])

    def test_timely_send_accepts_ack_after_proof_window(self):
        self.now = 4
        transport, websocket = self.transport([])
        sent = threading.Event()

        async def send(message):
            websocket.sent_values.append(json.loads(message))
            self.now = 6
            response = json.dumps({"id": 1, "result": {}})
            self.assertTrue(transport._inbound.try_put(response, len(response)))
            sent.set()

        websocket.send_str = send
        CodexAppServerClient(transport, initialized=True).interrupt_turn(
            thread_id="example-thread",
            turn_id="example-turn",
            deadline=10,
            send_start_deadline=5,
        )
        self.assertTrue(sent.is_set())
        self.assertEqual([m["method"] for m in websocket.sent_values], ["turn/interrupt"])

    def test_exact_five_second_admission_and_dequeue_boundary_is_inclusive(self):
        transport, websocket = self.transport([])
        self.now = 5
        transport.send_before({"id": 1, "method": "turn/interrupt"}, deadline=5)
        self.assertTrue(websocket.sent.wait(1))
        self.assertEqual(websocket.sent_values, [{"id": 1, "method": "turn/interrupt"}])

    def test_locked_aiohttp_uncompressed_send_writes_before_first_await(self):
        # An upgrade that introduces a pre-write await invalidates the local
        # initiation boundary; exercise the actual dependency, not a fake socket.
        from aiohttp import ClientWebSocketResponse
        from aiohttp.http_websocket import WebSocketWriter

        async def check():
            writes = []
            gate = asyncio.Event()

            class Protocol:
                _paused = True

                async def _drain_helper(self):
                    await gate.wait()

            transport = SimpleNamespace(write=writes.append, is_closing=lambda: False)
            writer = WebSocketWriter(
                cast(Any, Protocol()), cast(Any, transport), compress=0, limit=1
            )
            websocket = ClientWebSocketResponse.__new__(ClientWebSocketResponse)
            websocket._writer = writer
            send = websocket.send_str("example local write")
            try:
                pending = send.send(None)
                self.assertTrue(writes, "aiohttp yielded before uncompressed frame write")
                self.assertIsNotNone(pending, "fixture did not exercise backpressure")
            finally:
                send.close()

        asyncio.run(check())

    def test_response_timeout_does_not_allow_a_later_queued_write(self):
        transport, websocket = self.transport([])
        release = self.block_writer(transport, websocket)

        def timed_out(*, timeout=None):
            self.now = 11
            raise RpcError("example response timeout")

        with patch.object(transport, "receive", side_effect=timed_out):
            with self.assertRaises(RpcError):
                CodexAppServerClient(transport, initialized=True).interrupt_turn(
                    thread_id="example-thread",
                    turn_id="example-turn",
                    deadline=10,
                    send_start_deadline=50,
                )
        release()
        transport._thread.join(1)
        self.assertFalse(transport._thread.is_alive())
        self.assertEqual([m["id"] for m in websocket.sent_values], [0])
