from __future__ import annotations

import unittest
from typing import Any, cast

from hermes_codex_router.telegram_activity import telegram_activity


class Bot:
    def __init__(self) -> None:
        self.drafts: list[tuple[int, int, int]] = []
        self.actions: list[tuple[int, int]] = []

    def send_message_draft(self, chat_id: int, thread_id: int, *, draft_id: int) -> None:
        self.drafts.append((chat_id, thread_id, draft_id))

    def send_chat_action(self, chat_id: int, thread_id: int) -> None:
        self.actions.append((chat_id, thread_id))


class TelegramActivityTests(unittest.TestCase):
    def test_private_chat_uses_thinking_draft(self) -> None:
        bot = Bot()
        with telegram_activity(cast(Any, bot), chat_id=123456789, thread_id=1, message_id=42):
            pass
        self.assertEqual(bot.drafts, [(123456789, 1, 42)])
        self.assertEqual(bot.actions, [])

    def test_group_uses_chat_action(self) -> None:
        bot = Bot()
        with telegram_activity(cast(Any, bot), chat_id=-1001234567890, thread_id=77, message_id=42):
            pass
        self.assertEqual(bot.actions, [(-1001234567890, 77)])
        self.assertEqual(bot.drafts, [])


if __name__ == "__main__":
    unittest.main()
