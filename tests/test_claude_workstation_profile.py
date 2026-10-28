"""Workstation profile: whole-host readonly view, preauthorized foreground Bash (ADR 0081)."""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from hermes_codex_router import claude_permission_host as host
from hermes_codex_router.claude_bash_grants import BashGrantCache
from hermes_codex_router.claude_environment import claude_environment_prompt
from hermes_codex_router.claude_file_policy import file_tool_settings
from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig
from hermes_codex_router.claude_permission_protocol import event_digest
from hermes_codex_router.claude_permissions_config import parse_claude_file_permissions
from hermes_codex_router.state import StateError
from tests import test_claude_permission_host_roundtrip as fixtures
from tests.test_claude_permissions_config import ClaudePermissionsConfigTests


class WorkstationConfigTests(unittest.TestCase):
    def settings(self) -> dict[str, object]:
        return ClaudePermissionsConfigTests.settings(self)  # type: ignore[arg-type]

    def test_workstation_needs_no_extra_keys_and_rejects_visible_roots(self) -> None:
        settings = parse_claude_file_permissions(self.settings() | {"profile": "workstation"})
        assert settings is not None
        self.assertEqual(settings.profile, "workstation")
        with self.assertRaises(ValueError):
            parse_claude_file_permissions(
                self.settings() | {"profile": "workstation", "visible_roots": ["/usr"]}
            )

    def test_toolchain_is_available_to_workstation(self) -> None:
        tools = {"roots": ["/opt/example-tools"], "path": ["/opt/example-tools/bin"]}
        settings = parse_claude_file_permissions(
            self.settings() | {"profile": "workstation", "toolchain": tools}
        )
        assert settings is not None and settings.toolchain is not None


class WorkstationSandboxTests(unittest.TestCase):
    def sandbox(self, profile: str) -> FileToolSandboxConfig:
        with patch("hermes_codex_router.claude_file_sandbox.NamespaceRuntime"):
            return FileToolSandboxConfig(
                Path("/usr/bin/bwrap"),
                Path("/home/example/src/project"),
                Path("/home/example/session"),
                (Path("/opt/example-runtime"),),
                Path("/opt/example-runtime/claude"),
                Path("/opt/example-runtime/python"),
                Path("/opt/example-runtime/code"),
                Path("/run/example-permission.sock"),
                (Path("/home/example/private"),),
                profile=profile,
            )

    def test_only_workstation_shows_the_host_and_keeps_the_real_home(self) -> None:
        workstation = self.sandbox("workstation")
        self.assertTrue(workstation._namespace.host_view)
        self.assertEqual(workstation._namespace.sandbox_home, "/home/example/session")
        for profile in ("editor", "developer"):
            with self.subTest(profile=profile):
                self.assertFalse(self.sandbox(profile)._namespace.host_view)

    def test_prompt_describes_host_view_and_preauthorized_bash(self) -> None:
        prompt = claude_environment_prompt(self.sandbox("workstation"))
        self.assertIn("foreground Bash are preauthorized", prompt)
        self.assertIn("whole host filesystem is readable", prompt)
        self.assertNotIn("Bash until restart", prompt)

    def test_native_settings_route_bash_through_the_validating_hook(self) -> None:
        settings = json.loads(
            file_tool_settings(Path("/opt/example/python"), profile="workstation")
        )
        self.assertEqual(settings["permissions"]["ask"], ["Bash"])
        self.assertIn("Bash", settings["hooks"]["PermissionRequest"][0]["matcher"])


class WorkstationBashTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PermissionHostRoundtripTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.server.launch = dataclasses.replace(self.fixture.launch, profile="workstation")
        self.fixture.peer.release.set()
        self.cache_patch = patch.object(host, "BASH_GRANTS", BashGrantCache())
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def command(self, tool="Bash", tool_input=None, **changes):
        event = self.fixture._event(
            tool_name=tool,
            tool_input=tool_input or {"command": "printf example"},
            **changes,
        )
        wire = {
            "kind": "claude.permission.request",
            "version": 1,
            "nonce": str(uuid4()),
            "event": event,
            "eventDigest": event_digest(event),
        }
        return self.fixture.server._decision(self.fixture.state, wire)

    def test_validated_bash_is_journaled_allow_without_a_human_card(self):
        self.fixture.peer.decision = "deny"
        self.assertEqual(self.command(), "allow")
        self.assertEqual(self.command(tool_input={"command": "git status --short"}), "allow")
        self.assertEqual(self.fixture.peer.requests, [])
        self.assertEqual(self.fixture._statuses(), ["allow", "allow"])
        count = self.fixture.state._connection.execute(
            "SELECT count(*) FROM task_lifecycle_notices WHERE kind='claude_permission_wait'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_background_sandbox_bypass_and_authority_fields_still_refuse(self):
        for tool_input in (
            {"command": "pwd", "dangerouslyDisableSandbox": True},
            {"command": "pwd", "run_in_background": True},
            {"command": "pwd", "unknown": 1},
        ):
            with self.subTest(tool_input=tool_input), self.assertRaises(ValueError):
                self.command(tool_input=tool_input)
        with self.assertRaises(ValueError):
            self.command(permission_updates=[{"type": "allow"}])
        self.assertEqual(self.fixture.peer.requests, [])

    def test_writer_and_binding_checks_still_apply(self):
        self.fixture.state._connection.execute(
            "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
            (self.fixture.job.session_id,),
        )
        self.fixture.state._connection.commit()
        with self.assertRaises(StateError):
            self.command()
        self.assertEqual(self.fixture._statuses(), [])

    def test_prompted_file_operations_still_need_a_human(self):
        self.fixture.peer.decision = "deny"
        self.assertEqual(
            self.command("Write", {"file_path": "example.txt", "content": "x"}), "deny"
        )
        self.assertEqual(len(self.fixture.peer.requests), 1)


if __name__ == "__main__":
    unittest.main()
