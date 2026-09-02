from __future__ import annotations

import unittest
from unittest.mock import call as mock_call
from unittest.mock import patch

from hermes_codex_router.telegram import (
    TelegramBotApi,
    parse_direct_callback,
    parse_direct_message,
    parse_topic_callback,
    parse_topic_message,
)


class TelegramUpdateTests(unittest.TestCase):
    def test_chat_action_targets_forum_topic_and_general_without_fake_thread(self) -> None:
        telegram = TelegramBotApi("123456:example")
        with patch.object(telegram, "_call_with_timeout", return_value=True) as api_call:
            telegram.send_chat_action(-1001234567890, 77)
            telegram.send_chat_action(-1001234567890, 1)
        self.assertEqual(
            api_call.call_args_list,
            [
                mock_call(
                    "sendChatAction",
                    request_timeout=2,
                    chat_id=-1001234567890,
                    action="typing",
                    message_thread_id=77,
                ),
                mock_call(
                    "sendChatAction",
                    request_timeout=2,
                    chat_id=-1001234567890,
                    action="typing",
                ),
            ],
        )

    def test_thinking_draft_targets_private_chat_and_topic(self) -> None:
        telegram = TelegramBotApi("123456:example")
        with patch.object(telegram, "_call_with_timeout", return_value=True) as api_call:
            telegram.send_message_draft(123456789, 77, draft_id=42)

        api_call.assert_called_once_with(
            "sendMessageDraft",
            request_timeout=2,
            chat_id=123456789,
            draft_id=42,
            text="",
            message_thread_id=77,
        )

    def test_thinking_draft_rejects_group_chat(self) -> None:
        telegram = TelegramBotApi("123456:example")
        with self.assertRaisesRegex(Exception, "private"):
            telegram.send_message_draft(-1001234567890, 1, draft_id=42)

    def test_long_poll_adds_only_a_bounded_transport_margin(self) -> None:
        telegram = TelegramBotApi("123456:example")
        with patch.object(telegram, "_call_with_timeout", return_value=[]) as call:
            self.assertEqual(telegram.updates(offset=7, timeout=5), [])
        call.assert_called_once_with(
            "getUpdates",
            request_timeout=10,
            timeout=5,
            allowed_updates='["message", "callback_query"]',
            offset=7,
        )

    def test_ordinary_telegram_calls_use_shutdown_bounded_timeout(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def read(self) -> bytes:
                return b'{"ok":true,"result":{}}'

        observed: list[float] = []

        def opener(_request: object, *, timeout: float) -> Response:
            observed.append(timeout)
            return Response()

        telegram = TelegramBotApi("123456:example", opener=opener)
        telegram.call("getMe")
        self.assertEqual(observed, [8])

    def test_accepts_human_text_in_supergroup_topic(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 10,
                "message": {
                    "message_id": 20,
                    "message_thread_id": 77,
                    "is_topic_message": True,
                    "chat": {
                        "id": -1001234567890,
                        "type": "supergroup",
                        "title": "Example Project Alpha",
                    },
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

    def test_accepts_private_message_only_when_chat_matches_sender(self) -> None:
        update = {
            "update_id": 16,
            "message": {
                "message_id": 25,
                "chat": {"id": 123456789, "type": "private"},
                "from": {"id": 123456789, "is_bot": False},
                "text": "hello",
            },
        }
        parsed = parse_direct_message(update)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.chat_id, parsed.thread_id), (123456789, 1))
        update["message"]["chat"]["id"] = 987654321
        self.assertIsNone(parse_direct_message(update))

    def test_accepts_private_callback_only_when_chat_matches_sender(self) -> None:
        parsed = parse_direct_callback(
            {
                "update_id": 17,
                "callback_query": {
                    "id": "callback-direct",
                    "from": {"id": 123456789},
                    "data": "new:cancel:session",
                    "message": {
                        "message_id": 26,
                        "chat": {"id": 123456789, "type": "private"},
                    },
                },
            }
        )
        self.assertIsNotNone(parsed)

    def test_general_forum_topic_gets_stable_local_id(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 11,
                "message": {
                    "message_id": 21,
                    "chat": {
                        "id": -1001234567890,
                        "type": "supergroup",
                        "title": "Example Project Alpha",
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

    def test_extracts_bot_author_from_real_telegram_reply(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 13,
                "message": {
                    "message_id": 22,
                    "chat": {
                        "id": -1001234567890,
                        "type": "supergroup",
                        "title": "Example Project Beta",
                        "is_forum": True,
                    },
                    "from": {"id": 123456789, "is_bot": False},
                    "text": "relax, this is a connection test",
                    "reply_to_message": {
                        "message_id": 21,
                        "from": {
                            "id": 8752263516,
                            "is_bot": True,
                            "username": "example_antigravity_bot",
                        },
                        "text": "previous bot answer",
                    },
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.reply_to_username, "example_antigravity_bot")

    def test_forum_topic_anchor_is_not_treated_as_a_reply_to_its_bot_author(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 131,
                "message": {
                    "message_id": 23,
                    "message_thread_id": 77,
                    "is_topic_message": True,
                    "chat": {
                        "id": -1001234567890,
                        "type": "supergroup",
                        "title": "Example Project Beta",
                        "is_forum": True,
                    },
                    "from": {"id": 123456789, "is_bot": False},
                    "text": "second part of a burst",
                    "reply_to_message": {
                        "message_id": 77,
                        "from": {
                            "id": 8752263516,
                            "is_bot": True,
                            "username": "example_codex_bot",
                        },
                        "text": "topic created",
                    },
                },
            }
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed.reply_to_username)

    def test_text_quote_is_not_mistaken_for_telegram_reply(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 14,
                "message": {
                    "message_id": 23,
                    "chat": {
                        "id": -1001234567890,
                        "type": "supergroup",
                        "title": "Example Project Beta",
                        "is_forum": True,
                    },
                    "from": {"id": 123456789, "is_bot": False},
                    "text": "> previous bot answer\nrelax",
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed.reply_to_username)

    def test_manually_selected_telegram_quote_stays_with_active_agent(self) -> None:
        parsed = parse_topic_message(
            {
                "update_id": 15,
                "message": {
                    "message_id": 24,
                    "chat": {
                        "id": -1001234567890,
                        "type": "supergroup",
                        "title": "Example Project Beta",
                        "is_forum": True,
                    },
                    "from": {"id": 123456789, "is_bot": False},
                    "text": "commenting on this quote",
                    "reply_to_message": {
                        "message_id": 21,
                        "from": {
                            "id": 8752263516,
                            "is_bot": True,
                            "username": "example_antigravity_bot",
                        },
                        "text": "long previous bot answer",
                    },
                    "quote": {
                        "text": "selected fragment",
                        "position": 0,
                        "is_manual": True,
                    },
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed.reply_to_username)

    def test_marks_modern_and_legacy_forwards_as_quoted_context(self) -> None:
        modern = {
            "update_id": 16,
            "message": {
                "message_id": 25,
                "chat": {
                    "id": -1001234567890,
                    "type": "supergroup",
                    "title": "Example Project Beta",
                    "is_forum": True,
                },
                "from": {"id": 123456789, "is_bot": False},
                "text": "/stop",
                "forward_origin": {
                    "type": "user",
                    "sender_user": {"id": 8752263516, "is_bot": True},
                    "date": 1788220000,
                },
            },
        }
        legacy = {
            "update_id": 17,
            "message": {
                "message_id": 26,
                "chat": {
                    "id": -1001234567890,
                    "type": "supergroup",
                    "title": "Example Project Beta",
                    "is_forum": True,
                },
                "from": {"id": 123456789, "is_bot": False},
                "text": "/model",
                "forward_sender_name": "Example Bot",
                "forward_date": 1788220000,
            },
        }

        parsed_modern = parse_topic_message(modern)
        parsed_legacy = parse_topic_message(legacy)

        self.assertIsNotNone(parsed_modern)
        self.assertIsNotNone(parsed_legacy)
        assert parsed_modern is not None and parsed_legacy is not None
        self.assertTrue(parsed_modern.is_forwarded)
        self.assertTrue(parsed_legacy.is_forwarded)

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
