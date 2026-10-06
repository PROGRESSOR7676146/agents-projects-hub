from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router.codex_appserver import RpcRejectedError
from hermes_codex_router.supervisor import AppServerError, CodexAppServerSupervisor
from tests.test_codex_appserver import FakeTransport


class SupervisorFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.fallback = self.base / "codex"
        self.fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fallback.chmod(0o700)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_explicit_control_deadline_bounds_connect_and_initialize_without_fallback(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.transport_mode = "socket"
        transport = Mock()
        client = Mock()
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.time.monotonic", return_value=20),
            patch(
                "hermes_codex_router.supervisor.UnixWebSocketTransport", return_value=transport
            ) as connect,
            patch("hermes_codex_router.supervisor.CodexAppServerClient", return_value=client),
            patch("hermes_codex_router.supervisor.StdioJsonLineTransport.start") as fallback,
        ):
            self.assertIs(supervisor.client(allow_fallback=False, deadline=22), client)
            connect.assert_called_once_with(supervisor.socket_path, timeout=2)
            client.initialize.assert_called_once_with(deadline=22)
            client.initialize.side_effect = TimeoutError("Example initialize timeout")
            with self.assertRaises(TimeoutError):
                supervisor.client(allow_fallback=False, deadline=22)
            client.close.assert_called_once()
            fallback.assert_not_called()
        self.assertEqual(supervisor.transport_mode, "socket")

    def test_prefers_shared_socket_when_it_exists(self) -> None:
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

    def test_unmanaged_socket_keeps_logical_symlink_across_daemon_replacement(self) -> None:
        first = self.base / "old.sock"
        second = self.base / "new.sock"
        link = self.base / "shared.sock"
        first.touch()
        second.touch()
        link.symlink_to(first)
        supervisor = CodexAppServerSupervisor(
            link, manage_process=False, stdio_executable=self.fallback
        )
        link.unlink()
        link.symlink_to(second)
        self.assertEqual(supervisor.socket_path, link)
        self.assertEqual(supervisor.socket_path.resolve(), second)

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

    def test_background_socket_failure_does_not_change_active_transport(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.transport_mode = "socket"
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch(
                "hermes_codex_router.supervisor.UnixWebSocketTransport",
                side_effect=OSError("socket disappeared"),
            ),
        ):
            with self.assertRaises(OSError):
                supervisor.client(allow_fallback=False)
        self.assertEqual(supervisor.transport_mode, "socket")

    def test_first_control_client_cannot_select_a_headless_fallback(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "missing.sock", manage_process=False, stdio_executable=self.fallback
        )
        with (
            patch("hermes_codex_router.supervisor.StdioJsonLineTransport.start") as stdio,
            patch("hermes_codex_router.supervisor.CodexAppServerClient"),
        ):
            with self.assertRaises(AppServerError):
                supervisor.client(allow_fallback=False)
            stdio.assert_not_called()
        self.assertIsNone(supervisor.transport_mode)

    def test_first_control_client_uses_a_healthy_shared_socket_without_stdio(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport") as unix,
            patch("hermes_codex_router.supervisor.StdioJsonLineTransport.start") as stdio,
            patch("hermes_codex_router.supervisor.CodexAppServerClient") as client,
        ):
            self.assertIs(supervisor.client(allow_fallback=False), client.return_value)
            client.return_value.initialize.assert_called_once()
            unix.assert_called_once_with(supervisor.socket_path)
            stdio.assert_not_called()
        self.assertEqual(supervisor.transport_mode, "socket")

    def test_inaccessible_socket_selects_only_the_configured_productive_fallback(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        with patch.object(Path, "is_socket", side_effect=PermissionError("fictional denial")):
            supervisor.start()
            self.assertEqual(supervisor.transport_mode, "stdio-fallback")
            supervisor.transport_mode = "socket"
            with self.assertRaises(AppServerError):
                supervisor.client(allow_fallback=False)
        self.assertEqual(supervisor.transport_mode, "socket")

    def test_socket_appearing_during_start_has_an_explicit_transport_mode(self) -> None:
        supervisor = CodexAppServerSupervisor(self.base / "codex.sock", manage_process=False)
        with patch.object(Path, "is_socket", side_effect=(False, True)):
            supervisor.start()
        self.assertEqual(supervisor.transport_mode, "socket")

    def test_control_client_refuses_an_already_selected_fallback(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "missing.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.start()
        with (
            patch("hermes_codex_router.supervisor.StdioJsonLineTransport.start") as stdio,
            patch("hermes_codex_router.supervisor.CodexAppServerClient"),
        ):
            with self.assertRaises(AppServerError):
                supervisor.client(allow_fallback=False)
            stdio.assert_not_called()
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_inaccessible_socket_keeps_approvals_unavailable_and_recovery_safe(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        with patch.object(Path, "is_socket", side_effect=PermissionError("fictional denial")):
            self.assertFalse(supervisor.human_approvals_available())
            supervisor.transport_mode = "stdio-fallback"
            self.assertFalse(supervisor.restore_socket_at_idle())
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_failed_initialize_closes_probe_and_bounds_repeated_attempts(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.transport_mode = "stdio-fallback"
        probe = Mock()
        probe.initialize.side_effect = TimeoutError("fictional deadline")
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.time.monotonic", return_value=100) as clock,
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport") as transport,
            patch("hermes_codex_router.supervisor.CodexAppServerClient", return_value=probe),
        ):
            self.assertFalse(supervisor.restore_socket_at_idle())
            probe.close.assert_called_once()
            self.assertFalse(supervisor.restore_socket_at_idle())
            transport.assert_called_once()
            clock.return_value = 106
            self.assertFalse(supervisor.restore_socket_at_idle())
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(probe.close.call_count, 2)
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_failed_probe_cleanup_does_not_switch_or_escape(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.transport_mode = "stdio-fallback"
        probe = Mock()
        probe.close.side_effect = OSError("fictional close failure")
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport"),
            patch("hermes_codex_router.supervisor.CodexAppServerClient", return_value=probe),
        ):
            self.assertFalse(supervisor.restore_socket_at_idle())
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_client_construction_failure_closes_its_transport(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.transport_mode = "stdio-fallback"
        transport = Mock()
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport", return_value=transport),
            patch("hermes_codex_router.supervisor.CodexAppServerClient", side_effect=ValueError),
        ):
            self.assertFalse(supervisor.restore_socket_at_idle())
        transport.close.assert_called_once()
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_failed_client_initialization_closes_each_owned_transport(self) -> None:
        for mode, allow_fallback in (("stdio-fallback", True), ("socket", False)):
            with self.subTest(mode=mode, allow_fallback=allow_fallback):
                supervisor = CodexAppServerSupervisor(
                    self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
                )
                supervisor.transport_mode = mode
                transport = FakeTransport(
                    [{"id": 1, "error": {"code": -32603, "message": "fictional init rejection"}}]
                )
                with (
                    patch.object(Path, "is_socket", return_value=True),
                    patch.object(transport, "close") as close,
                    patch(
                        "hermes_codex_router.supervisor.UnixWebSocketTransport",
                        return_value=transport,
                    ),
                    patch(
                        "hermes_codex_router.supervisor.StdioJsonLineTransport.start",
                        return_value=transport,
                    ),
                ):
                    with self.assertRaises(RpcRejectedError):
                        supervisor.client(allow_fallback=allow_fallback)
                    close.assert_called_once()

    def test_failed_shared_initialize_is_closed_before_fallback_invocation(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.transport_mode = "socket"
        shared = FakeTransport(
            [{"id": 1, "error": {"code": -32603, "message": "fictional init rejection"}}]
        )
        fallback = FakeTransport(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "thread": {"id": "example-thread"},
                        "cwd": str(self.base),
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                    },
                },
            ]
        )
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch.object(shared, "close") as close,
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport", return_value=shared),
            patch("hermes_codex_router.supervisor.StdioJsonLineTransport.start") as start,
        ):

            def create_fallback(*args: object) -> FakeTransport:
                close.assert_called_once()
                return fallback

            start.side_effect = create_fallback
            client = supervisor.client()
            client.start_thread(cwd=self.base, model="example-model", project_id="example-project")
            self.assertEqual(fallback.sent[-1]["params"]["approvalPolicy"], "never")
            client.close()
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_failed_fallback_initialize_closes_both_transports(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.transport_mode = "socket"
        rejected = {"id": 1, "error": {"code": -32603, "message": "fictional rejection"}}
        shared, fallback = FakeTransport([rejected]), FakeTransport([rejected])
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch.object(shared, "close") as shared_close,
            patch.object(fallback, "close") as fallback_close,
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport", return_value=shared),
            patch(
                "hermes_codex_router.supervisor.StdioJsonLineTransport.start", return_value=fallback
            ),
        ):
            with self.assertRaises(RpcRejectedError):
                supervisor.client()
            shared_close.assert_called_once()
            fallback_close.assert_called_once()

    def test_client_construction_failure_closes_acquired_control_transport(self) -> None:
        supervisor = CodexAppServerSupervisor(self.base / "codex.sock", manage_process=False)
        supervisor.transport_mode = "socket"
        transport = Mock()
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport", return_value=transport),
            patch("hermes_codex_router.supervisor.CodexAppServerClient", side_effect=ValueError),
        ):
            with self.assertRaises(ValueError):
                supervisor.client(allow_fallback=False)
        transport.close.assert_called_once()

    def test_idle_fallback_recovers_only_after_shared_socket_initialize(self) -> None:
        socket_path = self.base / "codex.sock"
        supervisor = CodexAppServerSupervisor(
            socket_path, manage_process=False, stdio_executable=self.fallback
        )
        supervisor.start()
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

        class FakeClient:
            def __init__(self, transport: object, **kwargs: object) -> None:
                self.transport = transport
                self.approval_policy = kwargs.get("approval_policy", "on-request")

            def initialize(self, *, deadline: float | None = None) -> None:
                pass

            def close(self) -> None:
                pass

        with (
            patch.object(Path, "is_socket", return_value=True),
            patch(
                "hermes_codex_router.supervisor.UnixWebSocketTransport", return_value=object()
            ) as unix,
            patch(
                "hermes_codex_router.supervisor.StdioJsonLineTransport.start", return_value=object()
            ),
            patch("hermes_codex_router.supervisor.CodexAppServerClient", side_effect=FakeClient),
        ):
            # Ordinary client acquisition must not change an active fallback turn.
            self.assertEqual(getattr(supervisor.client(), "approval_policy"), "never")
            self.assertEqual(unix.call_count, 0)
            self.assertTrue(supervisor.restore_socket_at_idle())
            self.assertEqual(supervisor.transport_mode, "socket")
            self.assertEqual(getattr(supervisor.client(), "approval_policy"), "on-request")
            self.assertEqual(unix.call_count, 2)

    def test_idle_fallback_keeps_never_if_shared_socket_refuses_connection(self) -> None:
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        supervisor.start()
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch(
                "hermes_codex_router.supervisor.UnixWebSocketTransport",
                side_effect=OSError("refused"),
            ),
        ):
            self.assertFalse(supervisor.restore_socket_at_idle())
        self.assertEqual(supervisor.transport_mode, "stdio-fallback")

    def test_invisible_shared_socket_reports_unavailable_approvals_before_first_turn(
        self,
    ) -> None:
        # A worker whose namespace was built before the daemon directory existed
        # never sees the socket; it must not look healthy until a turn falls back.
        supervisor = CodexAppServerSupervisor(
            self.base / "codex.sock", manage_process=False, stdio_executable=self.fallback
        )
        self.assertIsNone(supervisor.transport_mode)
        self.assertFalse(supervisor.human_approvals_available())
        with patch.object(Path, "is_socket", return_value=True):
            self.assertTrue(supervisor.human_approvals_available())
            supervisor.transport_mode = "socket"
            self.assertTrue(supervisor.human_approvals_available())
            supervisor.transport_mode = "stdio-fallback"
            self.assertFalse(supervisor.human_approvals_available())

    def test_managed_server_does_not_claim_shared_approval_loss(self) -> None:
        supervisor = CodexAppServerSupervisor(self.base / "codex.sock", manage_process=True)
        self.assertTrue(supervisor.human_approvals_available())

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
