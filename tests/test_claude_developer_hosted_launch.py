"""Trusted configuration reaches the real per-turn host and durable profile pin.

Namespace preflight is replaced here; kernel/native custody has separate tests.
The permission host, journal, hook socket and signed fixture receipt are real.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import claude_permission_host as host
from hermes_codex_router import diagnostic_log, process_namespace
from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig
from hermes_codex_router.claude_mount_pins import MountPins
from hermes_codex_router.claude_permissions_config import ClaudeFilePermissionsConfig
from hermes_codex_router.external_runtime import ProviderUnavailableError
from hermes_codex_router.namespace_toolchain import NamespaceToolchain
from hermes_codex_router.process_namespace import NamespaceError, TrustScanLimitError
from hermes_codex_router.tlive_permissions import TlivePermissionConfig
from tests import test_claude_permission_host_roundtrip as fixtures


class DeveloperHostedLaunchTests(unittest.TestCase):
    def test_local_profile_binds_native_home_host_and_fresh_bash_receipt(self) -> None:
        fixture = fixtures.PermissionHostRoundtripTests()
        fixture.setUp(start_host=False)
        self.addCleanup(fixture.doCleanups)
        base = fixture.root.parent
        home = base / "provider-home"
        home.mkdir(mode=0o700)
        peer = fixtures.FakeTlive(base / "tlive.sock")
        self.addCleanup(peer.close)
        fixture.peer = peer
        settings = ClaudeFilePermissionsConfig(
            tlive_config=base / "protected.json",
            tlive_home=base / "tlive-home",
            provider_home=home,
            runtime_roots=(Path("/usr"),),
            python_executable=Path("/usr/bin/python3"),
            hook_code_root=Path("/opt/example-runtime"),
            bwrap_executable=Path("/usr/bin/bwrap"),
            private_paths=(base / "private",),
            profile="developer",
            toolchain=NamespaceToolchain((base / "tools",), (base / "tools/bin",)),
        )
        config = replace(fixture.config, claude_file_permissions=settings)
        transport = TlivePermissionConfig(
            str(peer.path), "42", "42", peer.request_key, peer.result_key
        )
        servers: list[host.PermissionServer] = []
        server_type = host.PermissionServer

        def capture(*args, **kwargs):
            server = server_type(*args, **kwargs)
            servers.append(server)
            return server

        with (
            patch.object(host, "load_tlive_permission_config", return_value=transport),
            patch.object(host.shutil, "which", return_value="/usr/bin/true"),
            patch("hermes_codex_router.claude_file_sandbox.NamespaceRuntime"),
            patch.object(FileToolSandboxConfig, "wrap", return_value=nullcontext()),
            patch.object(host, "PermissionServer", side_effect=capture),
            host.hosted_claude_launch(
                config,
                fixture.state,
                config.agents[0],
                fixture.job,
                fixture.token,
                fixture.native_id,
                fixture.root,
                is_new=True,
            ) as hosted,
        ):
            assert hosted is not None
            self.assertEqual(hosted.sandbox.profile, "developer")
            self.assertIs(hosted.sandbox.toolchain, settings.toolchain)
            self.assertIs(hosted.sandbox._namespace.toolchain, settings.toolchain)
            self.assertEqual(hosted.sandbox.provider_home, home / fixture.native_id)
            fixture.server = servers[0]
            fixture.launch = fixture.server.launch
            self.assertEqual(fixture.launch.profile, "developer")
            pinned = fixture.state._connection.execute(
                "SELECT mode, home_digest FROM claude_permission_session_modes"
            ).fetchone()
            expected = hashlib.sha256(
                ("hub.claude.session/developer/v1\x00" + str(home / fixture.native_id)).encode()
            ).hexdigest()
            self.assertEqual(tuple(pinned), ("file_tools", expected))
            hook, result = fixture._hook(
                fixture._event(tool_name="Bash", tool_input={"command": "pwd"})
            )
            self.assertEqual(fixture._await_request().tool_name, "Bash")
            fixture._finish(hook, result, "allow")
            self.assertEqual(fixture._statuses(), ["allow"])
        self.assertFalse(fixture.server.thread.is_alive())
        self.assertEqual(
            fixture.state._connection.execute(
                "SELECT status FROM claude_permission_launches"
            ).fetchone()[0],
            "closed",
        )

    def _refusal(self, error: Exception, *, logged: bool = True) -> tuple[str, list[str]]:
        fixture = fixtures.PermissionHostRoundtripTests()
        fixture.setUp(start_host=False)
        self.addCleanup(fixture.doCleanups)
        base = fixture.root.parent
        home = base / "provider-home"
        home.mkdir(mode=0o700)
        peer = fixtures.FakeTlive(base / "tlive.sock")
        self.addCleanup(peer.close)
        settings = ClaudeFilePermissionsConfig(
            tlive_config=base / "protected.json",
            tlive_home=base / "tlive-home",
            provider_home=home,
            runtime_roots=(Path("/usr"),),
            python_executable=Path("/usr/bin/python3"),
            hook_code_root=Path("/opt/example-runtime"),
            bwrap_executable=Path("/usr/bin/bwrap"),
            private_paths=(base / "private",),
            profile="developer",
            toolchain=NamespaceToolchain((base / "tools",), (base / "tools/bin",)),
        )
        config = replace(fixture.config, claude_file_permissions=settings)
        transport = TlivePermissionConfig(
            str(peer.path), "42", "42", peer.request_key, peer.result_key
        )
        servers: list[host.PermissionServer] = []
        server_type = host.PermissionServer

        def capture(*args, **kwargs):
            server = server_type(*args, **kwargs)
            servers.append(server)
            return server

        diagnostic_log.reset_repeat_state()
        self.addCleanup(diagnostic_log.reset_repeat_state)
        with (
            patch.object(host, "load_tlive_permission_config", return_value=transport),
            patch.object(host.shutil, "which", return_value="/usr/bin/true"),
            patch("hermes_codex_router.claude_file_sandbox.NamespaceRuntime"),
            patch.object(FileToolSandboxConfig, "wrap", side_effect=error),
            patch.object(host, "PermissionServer", side_effect=capture),
            ExitStack() as stack,
        ):
            logs = (
                stack.enter_context(self.assertLogs(diagnostic_log.LOGGER_NAME, level="WARNING"))
                if logged
                else None
            )
            failure = stack.enter_context(self.assertRaises(ProviderUnavailableError))
            with host.hosted_claude_launch(
                config,
                fixture.state,
                config.agents[0],
                fixture.job,
                fixture.token,
                fixture.native_id,
                fixture.root,
                is_new=True,
            ):
                self.fail("unverified boundary started productive execution")
        self.assertEqual(failure.exception.code, "claude_permission_host_unverified")
        self.assertIsNone(failure.exception.__cause__)
        self.assertEqual(len(servers), 1)
        self.assertFalse(servers[0].thread.is_alive())
        self.assertFalse(servers[0].path.exists())
        self.assertEqual(
            fixture.state._connection.execute(
                "SELECT status FROM claude_permission_launches"
            ).fetchone()[0],
            "closed",
        )
        return str(failure.exception), [] if logs is None else logs.output

    def test_trust_scan_limit_refusal_names_the_cause_without_exception_text(self) -> None:
        detail = "fictional-detail /home/example/project"
        message, logs = self._refusal(TrustScanLimitError(detail))
        self.assertEqual(
            message,
            "A directory tree the Claude file-tool boundary must inspect has more entries "
            "than the trust scan allows, for example a project or its Git metadata holding "
            "large caches. The productive turn was not started.",
        )
        self.assertEqual(
            logs,
            [
                "WARNING:hermes_codex_router:survived TrustScanLimitError "
                "at claude_permission_host.boundary_unverified"
            ],
        )

    def test_other_boundary_refusals_keep_the_generic_text_and_log_their_class(self) -> None:
        message, logs = self._refusal(NamespaceError("fictional-detail /home/example/secret"))
        self.assertEqual(
            message,
            "Claude file-tool permission boundary could not be verified. "
            "The productive turn was not started.",
        )
        self.assertEqual(
            logs,
            [
                "WARNING:hermes_codex_router:survived NamespaceError "
                "at claude_permission_host.boundary_unverified"
            ],
        )

    def test_a_failing_diagnostic_never_replaces_the_sanitized_refusal(self) -> None:
        before = diagnostic_log.dropped_records()
        with patch.object(diagnostic_log, "_record", side_effect=RuntimeError("fictional")):
            message, _ = self._refusal(NamespaceError("fictional-detail"), logged=False)
        self.assertIn("could not be verified", message)
        self.assertEqual(diagnostic_log.dropped_records(), before + 1)

    def test_real_writable_tree_scan_raises_the_limit_class(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-scan-limit-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            for index in range(4):
                (project / str(index)).mkdir()
            with (
                patch.object(process_namespace, "_MAX_WRITABLE_ENTRIES", 3),
                MountPins() as pins,
            ):
                descriptor = pins.open(project, directory=True)
                count = len(os.listdir("/proc/self/fd"))
                with self.assertRaises(TrustScanLimitError):
                    process_namespace._scan_writable_tree(project, descriptor)
                self.assertEqual(len(os.listdir("/proc/self/fd")), count)
