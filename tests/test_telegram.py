from __future__ import annotations

import unittest

from hermes_codex_router.telegram import parse_topic_callback, parse_topic_message


class TelegramUpdateTests(unittest.TestCase):
    def test_accepts_human_text_in_supergroup_topic(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 10,
                "message": {
                    "message_id": 20,
                    "message_thread_id": 77,
                    "is_topic_message": True,
                    "chat": {"id": -1001234567890, "type": "supergroup", "title": "Pythia"},
                    "from": {"id": 123456789, "is_bot": False},
                    "text": "pilot",
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.chat_id, parsed.thread_id), (-1001234567890, 77))

    def test_rejects_dm_non_topic_and_bot_messages(self) -> None:
        base = {
            "update_id": 10,
            "message": {
                "message_id": 20,
                "message_thread_id": 77,
                "is_topic_message": True,
                "chat": {"id": -1001234567890, "type": "supergroup"},
                "from": {"id": 123456789, "is_bot": True},
                "text": "loop",
            },
        }
        self.assertIsNone(parse_topic_message(base))
        base["message"]["from"]["is_bot"] = False
        base["message"]["chat"]["type"] = "private"
        self.assertIsNone(parse_topic_message(base))

    def test_general_forum_topic_gets_stable_local_id(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 11,
                "message": {
                    "message_id": 21,
                    "chat": {
                        "id": -1001234567890,
                        "type": "supergroup",
                        "title": "Pythia",
                        "is_forum": True,
                    },
                    "from": {"id": 123456789, "is_bot": False},
                    "text": "/pilot",
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.thread_id, 1)

    def test_parses_inline_model_callback(self) -> None:
        parsed = parse_topic_callback(
            {
                "update_id": 12,
                "callback_query": {
                    "id": "callback-1",
                    "from": {"id": 123456789},
                    "data": "model:gpt-5.6-sol",
                    "message": {
                        "message_id": 90,
                        "message_thread_id": 73,
                        "chat": {"id": -1001234567890, "type": "supergroup"},
                    },
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.data, "model:gpt-5.6-sol")
        self.assertEqual(parsed.thread_id, 73)


if __name__ == "__main__":
    unittest.main()
