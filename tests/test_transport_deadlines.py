from __future__ import annotations

import asyncio
import multiprocessing
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.codex_appserver import (
    RpcError,
    StdioJsonLineTransport,
    UnixWebSocketTransport,
)


class TransportDeadlineTests(unittest.TestCase):
    def test_silent_partial_stdio_line_has_deadline_and_owned_process_cleanup(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; print('{', end='', flush=True); time.sleep(2)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        transport = StdioJsonLineTransport(process)
        start = time.monotonic()
        try:
            with self.assertRaisesRegex(RpcError, "timed out"):
                transport.receive(timeout=0.05)
            self.assertLess(time.monotonic() - start, 1)
        finally:
            transport.close()
        self.assertIsNotNone(process.poll())

    def test_websocket_clean_close_wakes_reader_and_stops_sender(self) -> None:
        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def close(self):
                pass

            async def send_json(self, value):
                pass

            async def __aiter__(self):
                if False:
                    yield None

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def ws_connect(self, url):
                return Socket()

        transport = UnixWebSocketTransport.__new__(UnixWebSocketTransport)
        transport._socket_path = Path("/tmp/fictional-unused-socket")
        transport._inbound = queue.Queue()
        transport._outbound = queue.Queue()
        transport._ready = threading.Event()
        transport._timeout = 0.05

        def run() -> None:
            with (
                patch("hermes_codex_router.codex_appserver.aiohttp.UnixConnector"),
                patch(
                    "hermes_codex_router.codex_appserver.aiohttp.ClientSession",
                    return_value=Session(),
                ),
            ):
                asyncio.run(transport._run())
            with self.assertRaisesRegex(RpcError, "EOFError"):
                transport.receive(timeout=0.05)

        process = multiprocessing.get_context("fork").Process(target=run)
        process.start()
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join(2)
        self.assertEqual(process.exitcode, 0, "closed WebSocket stranded its sender")
