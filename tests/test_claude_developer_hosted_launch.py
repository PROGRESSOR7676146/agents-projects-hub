"""Trusted configuration reaches the real per-turn host and durable profile pin.

Namespace preflight is replaced here; kernel/native custody has separate tests.
The permission host, journal, hook socket and signed fixture receipt are real.
"""

from __future__ import annotations

import hashlib
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import claude_permission_host as host
from hermes_codex_router import diagnostic_log
from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig
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

    def _refusal(self, error: Exception) -> tuple[str, list[str]]:
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
        diagnostic_log.reset_repeat_state()
        self.addCleanup(diagnostic_log.reset_repeat_state)
        with (
            patch.object(host, "load_tlive_permission_config", return_value=transport),
            patch.object(host.shutil, "which", return_value="/usr/bin/true"),
            patch("hermes_codex_router.claude_file_sandbox.NamespaceRuntime"),
            patch.object(FileToolSandboxConfig, "wrap", side_effect=error),
            self.assertLogs(diagnostic_log.LOGGER_NAME, level="WARNING") as logs,
            self.assertRaises(ProviderUnavailableError) as failure,
        ):
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
        self.assertEqual(
            fixture.state._connection.execute(
                "SELECT status FROM claude_permission_launches"
            ).fetchone()[0],
            "closed",
        )
        return str(failure.exception), logs.output

    def test_trust_scan_limit_refusal_names_the_cause_without_paths(self) -> None:
        message, logs = self._refusal(
            TrustScanLimitError("writable tree exceeds trust scan limit: /home/example/project")
        )
        self.assertIn("too many files", message)
        self.assertNotIn("/home/example", message)
        self.assertEqual(len(logs), 1)
        self.assertIn(
            "survived TrustScanLimitError at claude_permission_host.boundary_unverified", logs[0]
        )
        self.assertNotIn("/home/example", logs[0])

    def test_other_boundary_refusals_keep_the_generic_text_and_log_their_class(self) -> None:
        message, logs = self._refusal(NamespaceError("private mount at /home/example/secret"))
        self.assertIn("could not be verified", message)
        self.assertNotIn("/home/example", message)
        self.assertIn(
            "survived NamespaceError at claude_permission_host.boundary_unverified", logs[0]
        )
        self.assertNotIn("/home/example", logs[0])
