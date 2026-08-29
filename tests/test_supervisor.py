from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.supervisor import AppServerError, CodexAppServerSupervisor


class SupervisorFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.fallback = self.base / "codex"
        self.fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fallback.chmod(0o700)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_prefers_shared_socket_when_multi_auth_is_healthy(self) -> None:
        socket_path = self.base / "codex.sock"
        socket_path.touch()
        with patch.object(Path, "is_socket", return_value=True):
            supervisor = CodexAppServerSupervisor(
                socket_path, manage_process=False, stdio_executable=self.fallback
            )
            supervisor.start()
            self.assertEqual(supervisor.transport_mode, "socket")

    def test_uses_official_stdio_when_shared_socket_is_down(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "missing.sock",
            manage_process=False,
            stdio_executable=self.fallback,
        )
        supervisor.start()
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_missing_socket_still_fails_without_fallback(self) -> None:
        supervisor = CodexAppServerSupervisor(self.base / "missing.sock", manage_process=False)
        with self.assertRaisesRegex(AppServerError, "unavailable"):
            supervisor.start()

    def test_stale_shared_socket_falls_back_when_connection_fails(self) -> None:
        socket_path = self.base / "codex.sock"
        socket_path.touch()
        supervisor = CodexAppServerSupervisor(
            socket_path, manage_process=False, stdio_executable=self.fallback
        )

        class FakeClient:
            def initialize(self) -> None:
                pass

        with (
            patch.object(Path, "is_socket", return_value=True),
            patch(
                "hermes_codex_router.supervisor.UnixWebSocketTransport",
                side_effect=OSError("connection refused"),
            ),
            patch(
                "hermes_codex_router.supervisor.StdioJsonLineTransport.start",
                return_value=object(),
            ),
            patch("hermes_codex_router.supervisor.CodexAppServerClient", return_value=FakeClient()),
        ):
            supervisor.start()
            client = supervisor.client()

        self.assertIsInstance(client, FakeClient)
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")


if __name__ == "__main__":
    unittest.main()
