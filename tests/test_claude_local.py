from __future__ import annotations

import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import claude_local
from hermes_codex_router.claude_local import (
    ClaudeSessionBusy,
    acquire_writer_lock,
    local_environment,
    read_route_environment,
)
from hermes_codex_router.claude_permissions_config import (
    ClaudeFilePermissionsConfig,
    parse_claude_file_permissions,
)
from hermes_codex_router.local_transfer import (
    CLAUDE_LOCAL_REFUSAL,
    LocalTransferError,
    local_resume_command,
)

NATIVE = "00000000-0000-4000-8000-000000000001"
TOKEN = "example-route-token"


class ClaudeLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.route = self.base / "route.environment"
        self.route.write_text(
            f"ANTHROPIC_BASE_URL=http://127.0.0.1:8317\nANTHROPIC_AUTH_TOKEN={TOKEN}\nOTHER=x\n"
        )
        self.route.chmod(0o600)
        (self.base / "sessions").mkdir(mode=0o700)
        self.home = self.base / "sessions" / NATIVE
        self.home.mkdir(mode=0o700)

    def permissions(self, **extra: str) -> ClaudeFilePermissionsConfig:
        permissions = parse_claude_file_permissions(
            {"profile": "host", "provider_home": str(self.base / "sessions"), **extra}
        )
        assert permissions is not None
        return permissions

    def test_host_profile_accepts_an_optional_route_environment(self) -> None:
        self.assertIsNone(self.permissions().route_environment)
        self.assertEqual(
            self.permissions(route_environment=str(self.route)).route_environment, self.route
        )
        with self.assertRaises(ValueError):
            self.permissions(route_environment="relative/path")

    def test_route_environment_is_private_complete_and_loopback(self) -> None:
        self.assertEqual(
            read_route_environment(self.route),
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": TOKEN},
        )
        self.route.chmod(0o644)
        with self.assertRaises(ValueError):
            read_route_environment(self.route)
        self.route.chmod(0o600)
        self.route.write_text("ANTHROPIC_BASE_URL=https://example.com\nANTHROPIC_AUTH_TOKEN=x\n")
        with self.assertRaises(ValueError):
            read_route_environment(self.route)
        self.route.write_text("ANTHROPIC_BASE_URL=http://127.0.0.1:8317\n")
        with self.assertRaises(ValueError):
            read_route_environment(self.route)
        for url in (
            "http://localhost.example.com:8317",
            "http://127.0.0.1.example.com:8317",
            "http://user@example.com@127.0.0.1:8317",
            "http://127.0.0.1@example.com:8317",
            "https://127.0.0.1:8317",
            "http://127.0.0.1",
        ):
            self.route.write_text(f"ANTHROPIC_BASE_URL={url}\nANTHROPIC_AUTH_TOKEN=x\n")
            with self.subTest(url=url), self.assertRaises(ValueError):
                read_route_environment(self.route)

    def test_local_environment_replaces_other_credentials_and_selects_the_session(self) -> None:
        environment = local_environment(
            {
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "other",
                "CLAUDE_CODE_OAUTH_TOKEN": "other",
                "CLAUDE_CONFIG_DIR": "/elsewhere",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_CODE_CHILD_SESSION": "1",
                "CLAUDECODE": "1",
                "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
            },
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": TOKEN},
            self.home,
        )
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.home))
        self.assertEqual(environment["CLAUDE_CODE_PROMPT_CACHE_TTL"], "5m")
        for key in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDECODE",
        ):
            self.assertNotIn(key, environment)

    def test_interactive_session_skips_first_run_screens_and_keeps_other_state(self) -> None:
        import json

        owner = self.base / "owner"
        (owner / ".claude").mkdir(parents=True)
        (owner / ".claude" / "settings.json").write_text('{"theme": "light"}')
        (self.home / ".claude.json").write_text('{"userID": "example", "projects": {}}')
        root = self.base / "project"
        claude_local.prepare_interactive_session(self.home, root, owner)
        state = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(state["userID"], "example")
        self.assertTrue(state["hasCompletedOnboarding"])
        project = state["projects"][str(root)]
        self.assertTrue(project["hasTrustDialogAccepted"])
        self.assertFalse(project["hasClaudeMdExternalIncludesApproved"])
        self.assertTrue(project["hasClaudeMdExternalIncludesWarningShown"])
        self.assertEqual(state["theme"], "light")
        settings = json.loads((self.home / "settings.json").read_text())
        self.assertEqual(settings, {"theme": "light", "skipDangerousModePermissionPrompt": True})
        self.assertEqual(stat.S_IMODE((self.home / "settings.json").stat().st_mode), 0o600)
        self.assertEqual(
            sorted(
                path.name
                for path in self.home.iterdir()
                if ".hub" in path.name or path.name.startswith(".settings")
            ),
            [],
        )

    def test_owner_theme_from_global_state_and_malformed_session_state_is_kept(self) -> None:
        import json

        owner = self.base / "owner"
        owner.mkdir()
        (owner / ".claude.json").write_text('{"theme": "light-daltonized"}')
        root = self.base / "project"
        claude_local.prepare_interactive_session(self.home, root, owner)
        self.assertEqual(
            json.loads((self.home / ".claude.json").read_text())["theme"], "light-daltonized"
        )
        (self.home / ".claude.json").write_text("{not json")
        with self.assertRaises(ValueError):
            claude_local.prepare_interactive_session(self.home, root, owner)
        self.assertEqual((self.home / ".claude.json").read_text(), "{not json")

    def test_writer_lock_admits_one_holder(self) -> None:
        first = acquire_writer_lock(self.home)
        try:
            with self.assertRaises(ClaudeSessionBusy):
                acquire_writer_lock(self.home)
        finally:
            os.close(first)
        os.close(acquire_writer_lock(self.home))

    def test_resume_command_needs_host_route_and_shows_no_secret(self) -> None:
        session_id = "example-hub-session"
        config = cast(
            Any,
            SimpleNamespace(
                source_path=self.base / "hub.json",
                claude_file_permissions=self.permissions(route_environment=str(self.route)),
            ),
        )
        command = local_resume_command(
            "claude", None, NATIVE, self.base, hub_config=config, hub_session_id=session_id
        )
        self.assertEqual(
            command.argv[-4:],
            (
                "hermes_codex_router.claude_local",
                "--config",
                str(self.base / "hub.json"),
                session_id,
            ),
        )
        self.assertNotIn(TOKEN, command.display)
        for broken in (
            replace(config.claude_file_permissions, route_environment=None),
            replace(config.claude_file_permissions, profile="workstation"),
        ):
            with (
                self.subTest(broken=broken.profile),
                self.assertRaises(LocalTransferError) as error,
            ):
                local_resume_command(
                    "claude",
                    None,
                    NATIVE,
                    self.base,
                    hub_config=cast(Any, replace_namespace(config, broken)),
                    hub_session_id=session_id,
                )
            self.assertEqual(str(error.exception), CLAUDE_LOCAL_REFUSAL)

    def test_model_and_effort_follow_hub_turns_or_refuse(self) -> None:
        self.assertEqual(
            claude_local.model_effort_arguments("claude-example", "xhigh"),
            ["--model", "claude-example", "--effort", "xhigh"],
        )
        self.assertEqual(claude_local.model_effort_arguments("default", "default"), [])
        with self.assertRaises(ValueError):
            claude_local.model_effort_arguments("claude-example", "turbo")

    def test_session_storage_must_stay_private(self) -> None:
        self.assertEqual(
            claude_local.host_session_home(self.base / "sessions", NATIVE, create=False), self.home
        )
        self.home.chmod(0o777)
        with self.assertRaises(ValueError):
            claude_local.host_session_home(self.base / "sessions", NATIVE, create=False)
        self.home.chmod(0o700)
        with self.assertRaises(ValueError):
            claude_local.host_session_home(self.base / "sessions", "../escape", create=False)

    def run_main(
        self, *, writer_mode: str = "local", busy: bool = False, flip_after_lock: bool = False
    ):
        permissions = self.permissions(route_environment=str(self.route))
        config = SimpleNamespace(
            claude_file_permissions=permissions,
            state_path=self.base / "state.db",
            codex_permission_profile=None,
            registry_path=self.base / "projects.json",
            require_agent=lambda agent_id: SimpleNamespace(runtime="claude"),
        )
        session = SimpleNamespace(
            agent_id="claude",
            writer_mode=writer_mode,
            provider_session_id=NATIVE,
            topic_id=7,
            model="claude-example",
            effort="high",
        )
        reads = [0]

        def get_session(_: str) -> SimpleNamespace:
            reads[0] += 1
            if flip_after_lock and reads[0] > 1:
                return SimpleNamespace(**{**vars(session), "writer_mode": "telegram"})
            return session

        state = SimpleNamespace(
            get_session=get_session, get_topic=lambda _: object(), close=lambda: None
        )
        prefix = self.base / "release" / "venv"
        (self.base / "release" / "bin").mkdir(parents=True, exist_ok=True)
        executable = self.base / "release" / "bin" / "claude"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)
        root = self.base / "project"
        root.mkdir(exist_ok=True)
        held = acquire_writer_lock(self.home) if busy else None
        calls: list[tuple[str, list[str], dict[str, str]]] = []
        try:
            with (
                patch("hermes_codex_router.hub_config.load_hub_config", return_value=config),
                patch("hermes_codex_router.state.HubState.open_existing", return_value=state),
                patch("hermes_codex_router.registry.load_registry", return_value=object()),
                patch(
                    "hermes_codex_router.topic_execution.resolve_topic_execution_root",
                    return_value=root,
                ),
                patch.object(claude_local.sys, "prefix", str(prefix)),
                patch.object(claude_local.os, "chdir") as chdir,
                patch.object(
                    claude_local.os,
                    "execve",
                    side_effect=lambda path, argv, env: calls.append((path, argv, env)),
                ),
            ):
                code = claude_local.main(["--config", str(self.base / "hub.json"), "hub-session"])
        finally:
            if held is not None:
                os.close(held)
        return code, calls, chdir

    def test_main_opens_the_exact_native_session_under_the_writer_lock(self) -> None:
        code, calls, chdir = self.run_main()
        self.assertEqual(code, 1)  # execve is patched, so main falls through
        ((path, argv, environment),) = calls
        self.assertEqual(path, str(self.base / "release" / "bin" / "claude"))
        self.assertEqual(
            argv[1:],
            [
                "--resume",
                NATIVE,
                "--permission-mode",
                "bypassPermissions",
                "--model",
                "claude-example",
                "--effort",
                "high",
            ],
        )
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.home))
        self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], TOKEN)
        chdir.assert_called_once_with(self.base / "project")
        with self.assertRaises(ClaudeSessionBusy):
            acquire_writer_lock(self.home)  # the lock stays held for the exec'd CLI

    def test_main_refuses_without_local_ownership_or_while_busy(self) -> None:
        code, calls, _ = self.run_main(writer_mode="telegram")
        self.assertEqual((code, calls), (1, []))
        code, calls, _ = self.run_main(busy=True)
        self.assertEqual((code, calls), (1, []))
        code, calls, _ = self.run_main(flip_after_lock=True)
        self.assertEqual((code, calls), (1, []))
        os.close(acquire_writer_lock(self.home))  # a refused hand-off releases the lock


def replace_namespace(namespace: SimpleNamespace, permissions: object) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(namespace), "claude_file_permissions": permissions})


if __name__ == "__main__":
    unittest.main()
