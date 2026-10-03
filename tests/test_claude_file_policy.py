from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from hermes_codex_router.claude_file_policy import (
    file_tool_settings,
    require_file_tool_event,
    validate_file_tool_input,
)
from hermes_codex_router.claude_stream import (
    ClaudeStreamError,
    ClaudeStreamReader,
    parse_claude_stream,
)


class FilePolicyTests(unittest.TestCase):
    def test_settings_have_only_permission_hook_and_no_persistent_allow(self) -> None:
        settings = json.loads(file_tool_settings(Path("/opt/example-runtime/bin/python")))
        self.assertEqual(set(settings["hooks"]), {"PermissionRequest"})
        self.assertEqual(settings["permissions"]["allow"], [])
        self.assertTrue(all(value is False for value in settings["enabledPlugins"].values()))
        self.assertIn(" -I -m hermes_codex_router.claude_permission_hook", str(settings))

    def test_native_file_events_do_not_forward_tool_results_or_reasoning(self) -> None:
        visible = []
        reader = ClaudeStreamReader(
            event_policy=require_file_tool_event, on_visible_assistant=visible.append
        )
        for event in (
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "example.txt"}}
                    ]
                },
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "RAW_EXAMPLE"}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "HIDDEN_EXAMPLE"}]},
            },
        ):
            reader.feed((json.dumps(event) + "\n").encode())
        self.assertEqual(visible, [])

    def test_unexpected_tools_plugins_children_and_modes_fail(self) -> None:
        events: tuple[dict[str, object], ...] = (
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "system", "subtype": "init", "plugins": [{"name": "example"}]},
            {"type": "system", "subtype": "init", "permissionMode": "auto"},
            {"type": "assistant", "parent_tool_use_id": "example-parent"},
            {"type": "system", "subtype": "hook_started", "hook_name": "Stop"},
            {"type": "task_started"},
        )
        for event in events:
            with self.subTest(event=event), self.assertRaises(ClaudeStreamError):
                require_file_tool_event(event)

    def test_prompted_targets_cannot_escape_through_paths_or_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "example-project"
            root.mkdir()
            (root / "escape").symlink_to(Path(directory) / "private")
            validate_file_tool_input("Write", {"file_path": "example.txt"}, root)
            for target in (
                "../private",
                "/proc/self/environ",
                "escape/key",
                "~/.claude/settings.json",
            ):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    validate_file_tool_input("Write", {"file_path": target}, root)
            with self.assertRaises(ValueError):
                validate_file_tool_input("Glob", {"pattern": "../*"}, root)

    def test_git_metadata_traversal_and_invisible_input_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested" / ".git").mkdir()
            (root / "alias").symlink_to(root / "nested" / ".git")
            for path in (
                ".git/hooks/example",
                "nested/.git/config",
                "nested/.GIT/config",
                "alias/config",
                "nested/../example.txt",
            ):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    validate_file_tool_input(
                        "Write", {"file_path": path, "content": "Example"}, root
                    )
            for content in (
                "\u061c",
                "\ufe0f",
                "\U000e0061",
                "\u2028",
                "\ue000",
                "\x85",
                2**53,
                1.5,
                "\u3164",
                "\u115f",
                "\u1160",
                "\uffa0",
                "\u034f",
                "\u17b4",
                "\u17b5",
                "\u180b",
                "\u180c",
                "\u180d",
                "\u180f",
                "\u2800",
                "\u00a0",
                "\u2000",
            ):
                with self.subTest(content=repr(content)), self.assertRaises(ValueError):
                    validate_file_tool_input(
                        "Write", {"file_path": "example.txt", "content": content}, root
                    )
            validate_file_tool_input(
                "Write", {"file_path": "example.txt", "content": "Русский\nEnglish\t"}, root
            )

    def test_large_tool_stream_is_validated_then_discarded_before_retention(self) -> None:
        native = str(uuid4())
        reader = ClaudeStreamReader(
            expected_session_id=native, event_policy=require_file_tool_event
        )
        tool_result = {
            "type": "user",
            "session_id": native,
            "message": {"content": [{"type": "tool_result", "content": "RAW_EXAMPLE" * 1024}]},
        }
        for _ in range(700):
            reader.feed((json.dumps(tool_result) + "\n").encode())
        reader.feed(
            (
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "Example complete",
                        "session_id": native,
                    }
                )
                + "\n"
            ).encode()
        )
        retained = reader.finish()
        self.assertLess(len(retained), 4096)
        self.assertNotIn("RAW_EXAMPLE", retained)
        self.assertEqual(
            parse_claude_stream(retained, event_policy=require_file_tool_event).text,
            "Example complete",
        )
        with self.assertRaises(ClaudeStreamError):
            reader.feed((json.dumps(tool_result) + "\n").encode())
