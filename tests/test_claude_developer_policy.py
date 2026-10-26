"""Explicit developer authority, with the existing editor still the default."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hermes_codex_router.claude_file_policy import (
    file_tool_settings,
    require_file_tool_event,
    validate_file_tool_input,
)
from hermes_codex_router.claude_stream import ClaudeStreamError


class ClaudeDeveloperPolicyTests(unittest.TestCase):
    def test_project_edits_are_preallowed_but_every_bash_is_explicitly_asked(self) -> None:
        settings = json.loads(
            file_tool_settings(Path("/opt/example-runtime/bin/python"), profile="developer")
        )
        permissions = settings["permissions"]
        self.assertEqual(set(permissions["allow"]), {"Read", "Glob", "Grep", "Write", "Edit"})
        self.assertEqual(permissions["ask"], ["Bash"])
        self.assertNotIn("Bash", permissions["allow"])
        self.assertIn("Agent", permissions["deny"])
        self.assertEqual(set(settings["hooks"]), {"PermissionRequest"})
        self.assertIn("Bash", settings["hooks"]["PermissionRequest"][0]["matcher"].split("|"))

    def test_editor_does_not_gain_command_or_preauthorized_write_authority(self) -> None:
        settings = json.loads(file_tool_settings(Path("/opt/example-runtime/bin/python")))
        self.assertEqual(settings["permissions"]["allow"], [])
        self.assertIn("Bash", settings["permissions"]["deny"])
        with self.assertRaises(ValueError):
            file_tool_settings(Path("/opt/example-runtime/bin/python"), profile="unknown")

    def test_readonly_and_mutating_commands_use_the_same_exact_validation(self) -> None:
        for command in (
            "pwd",
            "python -m unittest",
            "git add example.txt && git commit -m Example",
        ):
            with self.subTest(command=command):
                validate_file_tool_input(
                    "Bash",
                    {"command": command, "timeout": 120000},
                    Path("/home/example/project"),
                    profile="developer",
                )
                with self.assertRaises(ValueError):
                    validate_file_tool_input(
                        "Bash", {"command": command}, Path("/home/example/project")
                    )

    def test_background_bypass_unknown_and_unpreviewable_input_is_denied(self) -> None:
        for tool_input in (
            {},
            {"command": ""},
            {"command": "pwd", "run_in_background": True},
            {"command": "pwd", "dangerouslyDisableSandbox": True},
            {"command": "pwd", "unknown": False},
            {"command": "pwd", "timeout": True},
            {"command": "pwd", "timeout": -1},
            {"command": "pwd", "description": []},
            {"command": "pwd\u202e"},
            {"command": "pwd\x00"},
        ):
            with self.subTest(tool_input=tool_input), self.assertRaises(ValueError):
                validate_file_tool_input(
                    "Bash", tool_input, Path("/home/example/project"), profile="developer"
                )

    def test_stream_policy_exposes_only_the_requested_profile_tools(self) -> None:
        event = {"type": "system", "subtype": "init", "tools": ["Read", "Bash"]}
        require_file_tool_event(event, profile="developer")
        with self.assertRaises(ClaudeStreamError):
            require_file_tool_event(event)
        for extra in ({"tools": ["Agent"]}, {"mcp_servers": ["example"]}, {"skills": ["example"]}):
            with self.subTest(extra=extra), self.assertRaises(ClaudeStreamError):
                require_file_tool_event(event | extra, profile="developer")
