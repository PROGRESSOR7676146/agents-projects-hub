"""Real local WebSocket framing, with fictional traffic and no model/provider."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from hermes_codex_router.codex_appserver import RpcError, UnixWebSocketTransport
from hermes_codex_router.codex_inbox import BoundedInbox


class LocalWebSocketTests(unittest.TestCase):
    def test_real_unix_websocket_full_inbox_sends_and_drains_before_eof(self):
        try:
            left, right = socket.socketpair()
            with left, right:
                left.send(b"x")
        except PermissionError:
            if os.environ.get("HUB_REQUIRE_NAMESPACE_TESTS") == "1":
                self.fail("required local socket fixture unavailable")
            self.skipTest("local socket fixture unavailable")
        ready = threading.Event()
        full = threading.Event()
        received = threading.Event()
        failures = []
        controls = []

        class ObservedInbox(BoundedInbox):
            def try_put(self, value, size):
                result = super().try_put(value, size)
                if not result:
                    full.set()
                return result

        async def handler(request):
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)

            async def flood():
                for number in range(1100):
                    await websocket.send_json(
                        {"method": "example/foreign", "params": {"number": number}}
                    )

            producer = asyncio.create_task(flood())
            try:
                frame = await websocket.receive(timeout=3)
                message = json.loads(frame.data)
                if message != {"id": "example-request", "method": "example/rpc"}:
                    raise ValueError("fictional request mismatch")
                received.set()
                await producer
                await websocket.send_json({"id": "example-request", "result": {"complete": True}})
                await websocket.close()
            except Exception as error:
                failures.append(type(error).__name__)
            finally:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            return websocket

        async def serve(endpoint):
            stop = asyncio.Event()
            controls.append((asyncio.get_running_loop(), stop))
            application = web.Application()
            application.router.add_get("/", handler)
            runner = web.AppRunner(application, shutdown_timeout=1)
            await runner.setup()
            try:
                await web.UnixSite(runner, str(endpoint)).start()
                ready.set()
                await stop.wait()
            finally:
                await runner.cleanup()

        def run(endpoint):
            try:
                asyncio.run(serve(endpoint))
            except Exception as error:
                failures.append(type(error).__name__)
                ready.set()

        with tempfile.TemporaryDirectory(prefix="example-ws-") as directory:
            endpoint = Path(directory) / "example.sock"
            server = threading.Thread(target=run, args=(endpoint,), daemon=True)
            server.start()
            try:
                self.assertTrue(ready.wait(2))
                self.assertFalse(failures, "fictional socket server failed")
                with patch(
                    "hermes_codex_router.codex_transports.BoundedInbox",
                    return_value=ObservedInbox(max_frames=1),
                ):
                    transport = UnixWebSocketTransport(endpoint, timeout=2, max_pending_frames=1)
                try:
                    self.assertTrue(full.wait(2))
                    transport.send({"id": "example-request", "method": "example/rpc"})
                    self.assertTrue(received.wait(2), "full inbox blocked real outbound framing")
                    for number in range(1100):
                        self.assertEqual(
                            transport.receive(timeout=2),
                            {
                                "method": "example/foreign",
                                "params": {"number": number},
                            },
                        )
                    self.assertEqual(
                        transport.receive(timeout=2),
                        {
                            "id": "example-request",
                            "result": {"complete": True},
                        },
                    )
                    with self.assertRaisesRegex(RpcError, "EOFError"):
                        transport.receive(timeout=2)
                    transport._thread.join(2)
                    self.assertFalse(transport._thread.is_alive())
                finally:
                    transport.close()
            finally:
                for loop, stop in controls:
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(stop.set)
                server.join(3)
            self.assertFalse(server.is_alive(), "fictional socket server did not stop")
            self.assertFalse(failures, "fictional socket exchange failed")
