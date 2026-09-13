from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_codex_router.socket_ready import wait_for_unix_socket


class UnixSocketReadinessTests(unittest.TestCase):
    def bind(self, server: socket.socket, path: Path) -> None:
        try:
            server.bind(str(path))
        except PermissionError as exc:
            server.close()
            self.skipTest(f"Unix socket creation is blocked by the test sandbox: {exc}")

    def test_retries_a_refused_connection(self) -> None:
        refused = MagicMock()
        refused.connect.side_effect = ConnectionRefusedError
        ready = MagicMock()
        with patch(
            "hermes_codex_router.socket_ready.socket.socket",
            side_effect=(refused, ready),
        ):
            self.assertTrue(
                wait_for_unix_socket(
                    Path("/tmp/example.sock"),
                    timeout_seconds=0.1,
                    interval_seconds=0.001,
                )
            )
        refused.close.assert_called_once_with()
        ready.connect.assert_called_once_with("/tmp/example.sock")
        ready.close.assert_called_once_with()

    def test_stale_socket_inode_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app-server.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.bind(server, path)
            server.close()

            self.assertFalse(wait_for_unix_socket(path, timeout_seconds=0.1))

    def test_connectable_socket_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app-server.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.bind(server, path)
            server.listen(1)
            try:
                self.assertTrue(wait_for_unix_socket(path, timeout_seconds=0.2))
            finally:
                server.close()

    def test_waits_for_late_listener_instead_of_accepting_stale_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app-server.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.bind(stale, path)
            stale.close()

            def replace_with_listener() -> None:
                time.sleep(0.05)
                path.unlink()
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(path))
                server.listen(1)
                try:
                    time.sleep(0.3)
                finally:
                    server.close()

            thread = threading.Thread(target=replace_with_listener)
            thread.start()
            try:
                self.assertTrue(wait_for_unix_socket(path, timeout_seconds=0.25))
            finally:
                thread.join()


if __name__ == "__main__":
    unittest.main()
