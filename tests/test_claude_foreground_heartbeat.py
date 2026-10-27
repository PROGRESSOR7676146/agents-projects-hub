"""Native foreground heartbeats reference their own tool, not an agent parent."""

from __future__ import annotations

import json
import unittest
from uuid import uuid4

from hermes_codex_router.claude_file_policy import file_tool_event_policy
from hermes_codex_router.claude_stream import ClaudeStreamError, ClaudeStreamReader


def tool_start(tool_id: str, name: str = "Bash") -> dict[str, object]:
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name}]},
    }


def heartbeat(tool_id: str) -> dict[str, object]:
    # Native SDK emits these every 30s even with background tasks disabled.
    return {
        "type": "tool_progress",
        "tool_use_id": f"{tool_id}-heartbeat-0",
        "tool_name": "Bash",
        "parent_tool_use_id": tool_id,
        "elapsed_time_seconds": 30,
        "heartbeat": True,
        "session_id": str(uuid4()),
        "uuid": str(uuid4()),
    }


class ForegroundHeartbeatTests(unittest.TestCase):
    def test_owned_foreground_heartbeat_does_not_abort_or_forward_content(self) -> None:
        visible = []
        reader = ClaudeStreamReader(
            event_policy=file_tool_event_policy("developer"),
            on_visible_assistant=visible.append,
        )
        for event in (tool_start("example-tool"), heartbeat("example-tool")):
            reader.feed((json.dumps(event) + "\n").encode())
        self.assertEqual(visible, [])

    def test_unknown_completed_other_tool_and_other_reader_cannot_inherit_binding(self) -> None:
        start = tool_start("example-tool")
        for scenario in ("unknown", "completed", "other-tool", "other-reader"):
            with self.subTest(scenario=scenario):
                policy = file_tool_event_policy("developer")
                if scenario != "unknown":
                    policy(
                        tool_start("example-tool", "Read") if scenario == "other-tool" else start
                    )
                if scenario == "completed":
                    policy(
                        {
                            "type": "user",
                            "message": {
                                "content": [{"type": "tool_result", "tool_use_id": "example-tool"}]
                            },
                        }
                    )
                if scenario == "other-reader":
                    policy = file_tool_event_policy("developer")
                with self.assertRaises(ClaudeStreamError):
                    policy(heartbeat("example-tool"))

    def test_only_exact_bash_heartbeat_shape_is_accepted(self) -> None:
        invalid = (
            {"heartbeat": False},
            {"heartbeat": 1},
            {"tool_name": "Agent"},
            {"parent_tool_use_id": "another-tool"},
            {"tool_use_id": "example-tool-background-0"},
            {"tool_use_id": "example-tool-heartbeat-no-number"},
            {"elapsed_time_seconds": -1},
            {"elapsed_time_seconds": True},
            {"elapsed_time_seconds": 1.5},
            {"task_id": "example-task"},
            {"subagent_retry": {}},
            {"message": {"content": []}},
            {"session_id": []},
        )
        for patch in invalid:
            with self.subTest(patch=patch):
                policy = file_tool_event_policy("developer")
                policy(tool_start("example-tool"))
                with self.assertRaises(ClaudeStreamError):
                    policy(heartbeat("example-tool") | patch)

    def test_children_background_tasks_and_editor_remain_refused(self) -> None:
        policy = file_tool_event_policy("developer")
        policy(tool_start("example-tool"))
        events: tuple[dict[str, object], ...] = (
            tool_start("another-tool") | {"parent_tool_use_id": "example-tool"},
            {"type": "system", "subtype": "task_started"},
            {"type": "tool_progress", "tool_name": "Agent", "parent_tool_use_id": None},
        )
        for event in events:
            with self.subTest(event=event), self.assertRaises(ClaudeStreamError):
                policy(event)
        with self.assertRaises(ClaudeStreamError):
            file_tool_event_policy("editor")(heartbeat("example-tool"))
