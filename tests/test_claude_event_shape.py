from __future__ import annotations

import unittest
import uuid

from hermes_codex_router.claude_event_shape import MAX_SHAPE_CHARACTERS, claude_event_shape

SESSION = "00000000-0000-4000-8000-000000000001"


class ClaudeEventShapeTests(unittest.TestCase):
    def test_stop_hook_feedback_shape_names_protocol_fields_only(self) -> None:
        shape = claude_event_shape(
            {
                "type": "user",
                "isMeta": True,
                "parent_tool_use_id": None,
                "session_id": SESSION,
                "uuid": str(uuid.uuid4()),
                "message": {"role": "user", "content": "Example private continuation"},
            }
        )
        self.assertEqual(
            shape,
            "event type=user content=str meta=true parent=null "
            "keys=isMeta,message,parent_tool_use_id,session_id,type,uuid",
        )

    def test_lifecycle_and_block_types_are_named_without_values(self) -> None:
        self.assertEqual(
            claude_event_shape(
                {
                    "type": "command_lifecycle",
                    "state": "cancelled",
                    "session_id": SESSION,
                    "command_uuid": str(uuid.uuid4()),
                    "uuid": str(uuid.uuid4()),
                }
            ),
            "event type=command_lifecycle state=cancelled "
            "keys=command_uuid,session_id,state,type,uuid",
        )
        shape = claude_event_shape(
            {
                "type": "user",
                "isReplay": False,
                "message": {
                    "content": [
                        {"type": "text", "text": "Example private prompt"},
                        {"type": "tool_result", "content": "Example private output"},
                        {"type": "text", "text": "again"},
                    ]
                },
            }
        )
        self.assertEqual(
            shape,
            "event type=user content=list:text+tool_result replay=false keys=isReplay,message,type",
        )

    def test_untrusted_names_values_and_sizes_are_bounded(self) -> None:
        private = "Example private value /home/example/secret"
        shape = claude_event_shape(
            {
                "type": private,
                "subtype": 7,
                "isMeta": "yes",
                "parent_tool_use_id": "toolu_01ExamplePrivate",
                "message": {"content": [{"type": private}, "not-a-block"]},
                private: 1,
                **{f"field_{index:02d}": index for index in range(40)},
            }
        )
        self.assertNotIn("private", shape.casefold())
        self.assertNotIn("toolu", shape)
        self.assertIn("type=? subtype=? content=list:? meta=other parent=set", shape)
        self.assertTrue(shape.endswith(",+30"), shape)
        self.assertLessEqual(len(shape), MAX_SHAPE_CHARACTERS)
        self.assertEqual(claude_event_shape({}), "event type=absent")
        self.assertEqual(
            claude_event_shape({"type": "assistant", "message": "x" * 10_000}),
            "event type=assistant content=other keys=message,type",
        )


if __name__ == "__main__":
    unittest.main()
