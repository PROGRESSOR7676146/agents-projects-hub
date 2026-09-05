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

    def test_explicit_stdio_ignores_an_existing_shared_socket(self) -> None:
        socket_path = self.base / "codex.sock"
        socket_path.touch()
        with patch.object(Path, "is_socket", return_value=True):
            supervisor = CodexAppServerSupervisor(
                socket_path,
                manage_process=False,
                stdio_executable=self.fallback,
                configured_transport="stdio",
            )
            supervisor.start()

        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_uses_official_stdio_when_shared_socket_is_down(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "missing.sock",
            manage_process=False,
            stdio_executable=self.fallback,
        )
        supervisor.start()
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_uses_official_stdio_when_shared_multi_auth_upstream_is_down(self) -> None:
        socket_path = self.base / "codex.sock"
        socket_path.touch()
        with patch.object(Path, "is_socket", return_value=True):
            supervisor = CodexAppServerSupervisor(
                socket_path,
                manage_process=False,
                stdio_executable=self.fallback,
                shared_socket_health=lambda: False,
            )
            supervisor.start()
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_rechecks_shared_upstream_before_a_later_turn(self) -> None:
        socket_path = self.base / "codex.sock"
        socket_path.touch()
        healthy = True
        with patch.object(Path, "is_socket", return_value=True):
            supervisor = CodexAppServerSupervisor(
                socket_path,
                manage_process=False,
                stdio_executable=self.fallback,
                shared_socket_health=lambda: healthy,
            )
            supervisor.start()
            self.assertTrue(supervisor.ensure_shared_socket_health())
            healthy = False
            self.assertFalse(supervisor.ensure_shared_socket_health())

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
            patch(
                "hermes_codex_router.supervisor.CodexAppServerClient",
                return_value=FakeClient(),
            ) as client_factory,
        ):
            supervisor.start()
            client = supervisor.client()

        self.assertIsInstance(client, FakeClient)
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")
        calls = client_factory.call_args_list
        self.assertEqual(calls[-1].kwargs["approval_policy"], "never")

    def test_managed_server_never_unlinks_an_unowned_existing_socket_path(self) -> None:
        socket_path = self.base / "codex.sock"
        socket_path.write_text("owned elsewhere", encoding="utf-8")
        supervisor = CodexAppServerSupervisor(socket_path, manage_process=True)

        with self.assertRaisesRegex(AppServerError, "already exists"):
            supervisor.start()

        self.assertEqual(socket_path.read_text(encoding="utf-8"), "owned elsewhere")

    def test_managed_server_uses_an_exclusive_ownership_lock(self) -> None:
        socket_path = self.base / "codex.sock"
        first = CodexAppServerSupervisor(socket_path, manage_process=True)
        second = CodexAppServerSupervisor(socket_path, manage_process=True)
        first._acquire_socket_ownership()
        try:
            lock_metadata = (self.base / "codex.sock.lock").read_text(encoding="utf-8")
            self.assertIn("pid=", lock_metadata)
            self.assertIn("start=", lock_metadata)
            with self.assertRaisesRegex(AppServerError, "ownership lock"):
                second._acquire_socket_ownership()
        finally:
            first.stop()


if __name__ == "__main__":
    unittest.main()
