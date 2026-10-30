from __future__ import annotations

import hashlib
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from hermes_codex_router import diagnostics
from hermes_codex_router.claude_permission_host import hosted_claude_launch
from hermes_codex_router.claude_permissions_config import ClaudeFilePermissionsConfig
from hermes_codex_router.claude_permissions_journal import ClaudePermissionJournal
from hermes_codex_router.external_runtime import ProviderUnavailableError
from hermes_codex_router.hub_config import AgentDefinition, HubConfig, TerminalSettings
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.state import HubState
from tests import test_claude_permissions_journal as fixtures


class ClaudeSessionModePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudePermissionJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.home = fixture.root.parent / "session-homes"
        fixture.journal.bind_session_mode(
            fixture.job.job_id,
            fixture.token,
            fixture.launch.session_id,
            fixture.root,
            mode="file_tools",
            home=self.home / fixture.launch.session_id,
            is_new=True,
            profile="developer",
        )

    def check_modes(self, *, profile="developer", home=None, mode="file_tools", root=None):
        state = HubState.open_read_only(self.fixture.root.parent / "state.db")
        self.addCleanup(state.close)
        before = state._connection.total_changes
        result = ClaudePermissionJournal(state).session_mode_compatibility(
            agent_ids=("claude",),
            mode=mode,
            home=home or self.home,
            profile=profile,
            project_roots={"example-project": self.fixture.root},
            execution_root=lambda _: root or self.fixture.root,
        )
        self.assertEqual(state._connection.total_changes, before)
        return result

    def test_profile_rollout_is_detected_without_rewriting_the_saved_session(self) -> None:
        rows = self.fixture.state._connection.execute(
            "SELECT * FROM claude_permission_session_modes"
        ).fetchall()
        self.assertEqual(self.check_modes(), (1, 0))
        self.assertEqual(self.check_modes(profile="workstation"), (1, 1))
        self.assertEqual(self.check_modes(profile="editor"), (1, 1))
        self.assertEqual(
            self.fixture.state._connection.execute(
                "SELECT * FROM claude_permission_session_modes"
            ).fetchall(),
            rows,
        )

    def test_home_move_and_file_tool_disable_are_incompatible(self) -> None:
        self.assertEqual(self.check_modes(home=self.home / "moved"), (1, 1))
        self.assertEqual(self.check_modes(mode="text_only", profile="editor"), (1, 1))

    def test_satellite_is_checked_but_archived_sessions_are_not(self) -> None:
        connection = self.fixture.state._connection
        with connection:
            connection.execute("UPDATE agent_sessions SET status='satellite'")
        self.assertEqual(self.check_modes(profile="workstation"), (1, 1))
        with connection:
            connection.execute("UPDATE agent_sessions SET status='archived'")
        self.assertEqual(self.check_modes(profile="workstation"), (0, 0))

    def test_legacy_text_store_is_checked_using_the_registered_project_root(self) -> None:
        connection = self.fixture.state._connection
        store = self.fixture.root / "example-config"
        with connection:
            connection.execute(
                "UPDATE claude_permission_session_modes SET mode='text_only',home_digest=?",
                (hashlib.sha256(str(store).encode()).hexdigest(),),
            )
        self.assertEqual(
            self.check_modes(mode="text_only", profile="editor", home=Path("example-config")),
            (1, 0),
        )
        self.assertEqual(self.check_modes(), (1, 1))
        with connection:
            connection.execute("DELETE FROM claude_permission_session_modes")
        self.assertEqual(
            self.check_modes(mode="text_only", profile="editor", home=Path("example-config")),
            (1, 0),
        )
        self.assertEqual(self.check_modes(), (1, 1))

    def test_relative_text_store_uses_the_sessions_own_execution_root(self) -> None:
        lane = self.fixture.root.parent / "example-worktree"
        lane.mkdir()
        relative = Path("example-config")
        connection = self.fixture.state._connection
        with connection:
            connection.execute(
                "UPDATE claude_permission_session_modes SET mode='text_only',home_digest=?",
                (hashlib.sha256(str(lane / relative).encode()).hexdigest(),),
            )
        self.assertEqual(
            self.check_modes(mode="text_only", profile="editor", home=relative, root=lane),
            (1, 0),
        )
        self.assertEqual(
            self.check_modes(mode="text_only", profile="editor", home=relative), (1, 1)
        )

    def config(self) -> HubConfig:
        root = self.fixture.root
        settings = ClaudeFilePermissionsConfig(
            tlive_config=root / "protected.json",
            tlive_home=root / "tlive",
            provider_home=self.home,
            runtime_roots=(Path("/usr"),),
            python_executable=Path("/usr/bin/python3"),
            hook_code_root=root / "runtime",
            bwrap_executable=Path("/usr/bin/bwrap"),
            private_paths=(root / "private",),
            profile="workstation",
        )
        return HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=root / "projects.json",
            state_path=root.parent / "state.db",
            codex_socket_path=root / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(
                AgentDefinition(
                    "claude",
                    "Claude",
                    "example_bot",
                    "claude",
                    None,
                    False,
                    False,
                    "example-model",
                    "high",
                    executable="/usr/bin/true",
                ),
            ),
            claude_file_permissions=settings,
        )

    def test_doctor_reports_the_mismatch_as_required_without_disclosing_native_binding(
        self,
    ) -> None:
        config = self.config()
        registry = ProjectRegistry(
            1,
            (self.fixture.root.parent,),
            (Project("example-project", "Example", "Example", self.fixture.root),),
        )
        with (
            patch.object(diagnostics, "load_registry", return_value=registry),
            patch.object(
                diagnostics, "_command", return_value=diagnostics.Check("command", True, "ok")
            ),
            patch.object(
                diagnostics, "_socket_check", return_value=diagnostics.Check("socket", True, "ok")
            ),
            patch.object(
                diagnostics,
                "probe_codex_config_proxy",
                return_value=SimpleNamespace(ok=True, detail="ok"),
            ),
        ):
            report = diagnostics.run_doctor(config)
            self.assertFalse(report["ok"])
            self.assertNotIn(self.fixture.launch.session_id, str(report))
            self.assertNotIn(str(self.home), str(report))
            checks = cast(list[dict[str, object]], report["checks"])
            check = next(item for item in checks if item["name"] == "claude_session_modes")
            self.assertFalse(check["ok"])
            self.assertTrue(check["required"])
            self.assertIn("checked=1 incompatible=1", cast(str, check["detail"]))
            assert config.claude_file_permissions is not None
            compatible = replace(
                config,
                claude_file_permissions=replace(
                    config.claude_file_permissions, profile="developer"
                ),
            )
            self.assertTrue(diagnostics.run_doctor(compatible)["ok"])
            lane = self.fixture.root.parent / "example-linked-root"
            lane.mkdir()
            with self.fixture.state._connection as connection:
                connection.execute(
                    "UPDATE claude_permission_session_modes SET mode='text_only',home_digest=?",
                    (hashlib.sha256(str(lane / "relative-config").encode()).hexdigest(),),
                )
            with (
                patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "relative-config"}),
                patch.object(
                    diagnostics, "resolve_topic_execution_root", return_value=lane
                ) as resolve,
            ):
                text = replace(config, claude_file_permissions=None)
                self.assertTrue(diagnostics.run_doctor(text)["ok"])
                resolve.assert_called_once()
                self.assertEqual(resolve.call_args.args[2].topic_id, self.fixture.job.topic_id)

    def test_refusal_explains_preserved_conversation_before_any_host_or_provider_start(
        self,
    ) -> None:
        config = self.config()
        fixture = self.fixture
        with (
            patch(
                "hermes_codex_router.claude_permission_host.load_tlive_permission_config"
            ) as host,
            self.assertRaises(ProviderUnavailableError) as failure,
        ):
            with hosted_claude_launch(
                config,
                fixture.state,
                config.agents[0],
                fixture.job,
                fixture.token,
                fixture.launch.session_id,
                fixture.root,
            ):
                self.fail("incompatible session was resumed")
        host.assert_not_called()
        self.assertEqual(failure.exception.code, "claude_session_mode_changed")
        self.assertIn("access profile", failure.exception.public_message)
        self.assertIn("conversation was preserved", failure.exception.public_message)


if __name__ == "__main__":
    unittest.main()
