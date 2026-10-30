from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.claude_environment import claude_environment_prompt
from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig
from hermes_codex_router.external_runtime import ExternalCliAdapter, ExternalRuntimeError
from hermes_codex_router.namespace_toolchain import NamespaceToolchain


class ClaudeEnvironmentTests(unittest.TestCase):
    def sandbox(self, profile: str = "developer") -> FileToolSandboxConfig:
        with patch("hermes_codex_router.claude_file_sandbox.NamespaceRuntime"):
            return FileToolSandboxConfig(
                Path("/usr/bin/bwrap"),
                Path("/home/example/project"),
                Path("/home/example/session"),
                (Path("/opt/example-runtime"),),
                Path("/opt/example-runtime/claude"),
                Path("/opt/example-runtime/python"),
                Path("/opt/example-runtime/code"),
                Path("/run/example-permission.sock"),
                (Path("/home/example/private"),),
                profile=profile,
            )

    def test_editor_does_not_inherit_developer_or_consultation_tools(self) -> None:
        prompt = claude_environment_prompt(self.sandbox("editor"))
        self.assertIn("Read", prompt)
        self.assertIn("Write/Edit require", prompt)
        self.assertNotIn("Bash until restart", prompt)
        self.assertNotIn("hub-consult", prompt)

    def test_developer_uses_actual_configured_interpreter_without_private_paths(self) -> None:
        toolchain = NamespaceToolchain(
            (Path("/opt/example-tools"),),
            (Path("/opt/example-tools/python/bin"),),
            git_python=Path("/opt/example-tools/python/bin/python3"),
        )
        with patch("hermes_codex_router.claude_file_sandbox.NamespaceRuntime"):
            configured = replace(self.sandbox(), toolchain=toolchain)
        prompt = claude_environment_prompt(configured)
        self.assertIn("/opt/example-tools/python/bin/python3", prompt)
        self.assertIn("Bash until restart", prompt)
        self.assertIn("AGENTS.md", prompt)
        self.assertIn("foreground", prompt)
        self.assertIn("unknown", prompt)
        self.assertIn("Configured Git/hook Python", prompt)
        self.assertIn("sys.prefix", prompt)
        self.assertNotIn("Configured development Python", prompt)
        self.assertNotIn("hub-consult astra", prompt)
        self.assertNotIn("hub-consult sol", prompt)
        self.assertNotIn("/home/example/private", prompt)
        self.assertNotIn("/run/example-permission.sock", prompt)
        self.assertNotIn("/home/example/session", prompt)

    def test_toolchain_omission_does_not_invent_available_programs(self) -> None:
        prompt = claude_environment_prompt(self.sandbox())
        self.assertNotIn("hub-consult", prompt)
        self.assertNotIn("Configured development Python", prompt)

    def test_blank_request_is_rejected_before_orientation_or_launch(self) -> None:
        adapter = ExternalCliAdapter("claude")
        for profile in ("editor", "developer"):
            with (
                self.subTest(profile=profile),
                tempfile.TemporaryDirectory() as directory,
                patch.object(adapter, "_run_owned_claude_turn") as run,
                self.assertRaises(ExternalRuntimeError),
            ):
                adapter.run_turn(
                    cwd=Path(directory), prompt=" \n\t", claude_sandbox=self.sandbox(profile)
                )
            run.assert_not_called()

    def test_real_adapter_passes_current_profile_note_with_each_resumed_request(self) -> None:
        adapter = ExternalCliAdapter("claude")
        sandbox = self.sandbox("editor")
        native = "00000000-0000-4000-8000-000000000001"
        terminal = (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": native,
                    "result": "done",
                }
            )
            + "\n"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "_turn_environment", return_value={}),
            patch.object(adapter, "_prepare_claude_input", return_value=None),
            patch.object(
                adapter,
                "_run_owned_claude_turn",
                return_value=subprocess.CompletedProcess([], 0, terminal, ""),
            ) as run,
        ):
            adapter.run_turn(
                cwd=Path(directory),
                prompt="CURRENT REQUEST",
                session_id=native,
                claude_sandbox=sandbox,
            )
        argv = run.call_args.args[0]
        self.assertIn("Profile: editor.", argv[-1])
        self.assertTrue(argv[-1].endswith("CURRENT REQUEST"))
        self.assertNotIn("Bash until restart", argv[-1])
