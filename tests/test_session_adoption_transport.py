from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.codex_appserver import RpcError, UnixWebSocketTransport


class SessionAdoptionTransportTests(unittest.TestCase):
    def test_connection_timeout_joins_owned_transport_thread(self) -> None:
        # asyncio wakeups use this local socket pair; restricted sandboxes can
        # permit creation but deny send, silently stranding call_soon_threadsafe.
        try:
            left, right = socket.socketpair()
            with left, right:
                left.send(b"x")
        except PermissionError as exc:
            self.skipTest(f"asyncio wakeup sockets are blocked by the sandbox: {exc}")
        holders = []

        class StalledTransport(UnixWebSocketTransport):
            async def _run(self) -> None:
                # No socket/network: block the fictional connect phase itself.
                loop = asyncio.get_running_loop()
                task = asyncio.current_task()
                holders.append((self, loop, task))
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            endpoint = Path(directory) / "fictional.sock"
            endpoint.touch()
            try:
                with self.assertRaises(RpcError):
                    StalledTransport(endpoint, timeout=0.05)
                self.assertEqual(len(holders), 1)
                self.assertFalse(holders[0][0]._thread.is_alive())
            finally:
                for transport, loop, task in holders:
                    if transport._thread.is_alive() and task is not None:
                        loop.call_soon_threadsafe(task.cancel)
                        transport._thread.join(timeout=1)
