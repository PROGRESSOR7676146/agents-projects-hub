from __future__ import annotations

import socket
import threading
import unittest
from unittest.mock import Mock

from hermes_codex_router.codex_transports import UnixJsonLineTransport


class CodexJsonLineCloseTests(unittest.TestCase):
    def test_shutdown_refusal_is_reported_before_taking_stream_locks(self) -> None:
        connection, peer = socket.socketpair()
        transport = UnixJsonLineTransport(connection)
        denied = Mock(wraps=connection)
        denied.shutdown.side_effect = PermissionError(1, "Example shutdown denied")
        transport._connection = denied
        try:
            with self.assertRaises(PermissionError):
                transport.close()
            self.assertFalse(transport._reader.closed)
            self.assertFalse(transport._writer.closed)
        finally:
            transport._connection = connection
            transport.close()
            peer.close()

    def test_close_wakes_owned_blocked_reader_without_closing_peer_listener(self) -> None:
        connection, peer = socket.socketpair()
        transport = UnixJsonLineTransport(connection)
        reading = threading.Event()
        received = threading.Event()
        closed = threading.Event()
        failures: list[BaseException] = []

        def receive() -> None:
            reading.set()
            try:
                transport.receive()
            except BaseException as error:
                failures.append(error)
            finally:
                received.set()

        def close() -> None:
            try:
                transport.close()
            finally:
                closed.set()

        reader = threading.Thread(target=receive, daemon=True)
        closer = threading.Thread(target=close, daemon=True)
        reader.start()
        try:
            self.assertTrue(reading.wait(1))
            self.assertFalse(received.wait(0.05))
            closer.start()
            self.assertTrue(closed.wait(1), "close must wake the reader before taking TextIO locks")
            self.assertTrue(received.wait(1))
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], EOFError)
            self.assertEqual(peer.recv(1), b"")
            self.assertGreaterEqual(peer.fileno(), 0)
            transport.close()
        finally:
            # Test-only release also bounds the regression against the broken
            # ordering, without leaving a blocked thread in the test process.
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.close()
            reader.join(2)
            if closer.ident is not None:
                closer.join(2)
            transport.close()
