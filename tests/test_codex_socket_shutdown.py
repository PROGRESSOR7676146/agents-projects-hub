from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError
from hermes_codex_router.codex_transports import UnixWebSocketTransport
from tests import test_codex_transport_backpressure as scripted


class SocketShutdownTests(unittest.TestCase):
    def transport(self):
        temporary = tempfile.TemporaryDirectory(prefix="example-socket-shutdown-")
        self.addCleanup(temporary.cleanup)
        endpoint = Path(temporary.name) / "example.sock"
        endpoint.touch()
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        self.addCleanup(release_cleanup.set)

        class DelayedSession(scripted.ScriptedSession):
            async def __aexit__(self, *args):
                cleanup_entered.set()
                release_cleanup.wait()

        session = DelayedSession(scripted.ScriptedSocket([]))
        with (
            patch("hermes_codex_router.codex_transports.aiohttp.UnixConnector"),
            patch(
                "hermes_codex_router.codex_transports.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            transport = UnixWebSocketTransport(endpoint, timeout=2)
        join = transport._thread.join

        def cleanup():
            release_cleanup.set()
            try:
                transport.close()
            finally:
                join(timeout=2)
                self.assertFalse(transport._thread.is_alive(), "fixture leaked its thread")

        self.addCleanup(cleanup)
        return transport, cleanup_entered, release_cleanup, join

    def test_stalled_cleanup_reports_unconfirmed_shutdown_and_refuses_new_sends(self):
        transport, entered, _, join = self.transport()
        with patch.object(transport._thread, "join", side_effect=lambda timeout: join(timeout=0)):
            with self.assertRaisesRegex(RpcError, "shutdown is unconfirmed"):
                transport.close()
        self.assertTrue(entered.wait(2))
        self.assertTrue(transport._thread.is_alive())
        with self.assertRaisesRegex(RpcError, "is closed"):
            transport.send({"method": "example-never-sent"})

    def test_repeat_close_cannot_turn_a_still_live_thread_into_success(self):
        transport, entered, _, join = self.transport()
        with patch.object(transport._thread, "join", side_effect=lambda timeout: join(timeout=0)):
            try:
                transport.close()
            except RpcError:
                pass
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(RpcError, "shutdown is unconfirmed"):
                transport.close()
        self.assertTrue(transport._thread.is_alive())

    def test_repeat_close_waits_for_late_cleanup_instead_of_trusting_closed_flag(self):
        transport, entered, release, join = self.transport()
        with patch.object(transport._thread, "join", side_effect=lambda timeout: join(timeout=0)):
            try:
                transport.close()
            except RpcError:
                pass
        self.assertTrue(entered.wait(2))

        def complete_cleanup(timeout):
            release.set()
            join(timeout=timeout)

        with patch.object(transport._thread, "join", side_effect=complete_cleanup):
            transport.close()
        self.assertFalse(transport._thread.is_alive())
        transport.close()

    def test_unconfirmed_close_preserves_accepted_frames_and_wakes_empty_receiver(self):
        transport, _, _, join = self.transport()
        frame = json.dumps({"method": "example-accepted"})
        self.assertTrue(transport._inbound.try_put(frame, len(frame)))
        with patch.object(transport._thread, "join", side_effect=lambda timeout: join(timeout=0)):
            try:
                transport.close()
            except RpcError:
                pass
        self.assertEqual(transport.receive(timeout=0), {"method": "example-accepted"})
        with self.assertRaisesRegex(RpcError, "EOFError"):
            transport.receive(timeout=0)

    def test_client_propagates_unconfirmed_close_on_every_attempt(self):
        transport, _, _, join = self.transport()
        client = CodexAppServerClient(transport)
        with patch.object(transport._thread, "join", side_effect=lambda timeout: join(timeout=0)):
            for _ in range(2):
                with self.assertRaisesRegex(RpcError, "shutdown is unconfirmed"):
                    client.close()

    def test_normal_close_and_repeated_close_confirm_physical_thread_exit(self):
        transport, _, release, _ = self.transport()
        release.set()
        transport.close()
        self.assertFalse(transport._thread.is_alive())
        transport.close()
        self.assertFalse(transport._thread.is_alive())
