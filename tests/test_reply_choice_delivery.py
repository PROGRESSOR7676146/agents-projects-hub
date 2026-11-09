"""Choice buttons through the real outbox and final sender; fictional Telegram."""

from __future__ import annotations

import unittest
from typing import Any

from hermes_codex_router.metadata import format_agent_response
from hermes_codex_router.reply_choices import CHOICE_PART_MARKER, CHOICE_PROMPT_HTML
from tests import test_outbox_sender as fixtures


class MarkupBot(fixtures.Bot):
    def __init__(self) -> None:
        super().__init__()
        self.markups: list[object] = []
        self.next_id = 500

    def send_html(self, *args: Any, **kwargs: Any) -> int:
        super().send_html(*args, **{k: v for k, v in kwargs.items() if k != "reply_markup"})
        self.markups.append(kwargs.get("reply_markup"))
        self.next_id += 1
        return self.next_id


class ReplyChoiceDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_answer_is_sent_first_then_buttons_without_the_marker(self) -> None:
        html = format_agent_response("Which option?\n[[choices: Yes | No]]", {"Agent": "Example"})
        job_id = self.fixture.ready_outbox("opencode", 81, telegram_html=html)
        bot = MarkupBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        try:
            self.assertTrue(sender._deliver_one("opencode"))
            self.assertTrue(sender._deliver_one("opencode"))
            self.assertFalse(sender._deliver_one("opencode"))
            texts = [text for _, _, text in bot.sent]
            self.assertEqual(len(texts), 2)
            self.assertIn("Which option?", texts[0])
            self.assertNotIn("choices", texts[0])
            self.assertNotIn(CHOICE_PART_MARKER, "".join(texts))
            self.assertEqual(texts[1], CHOICE_PROMPT_HTML)
            self.assertIsNone(bot.markups[0])
            self.assertEqual(
                bot.markups[1],
                {
                    "inline_keyboard": [
                        [{"text": "Yes", "callback_data": "ch:0"}],
                        [{"text": "No", "callback_data": "ch:1"}],
                    ]
                },
            )
            outbox = sender.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(outbox.status, "delivered")
        finally:
            sender.close()

    def test_answer_without_choices_is_one_unchanged_part(self) -> None:
        html = format_agent_response("Plain answer", {"Agent": "Example"})
        self.fixture.ready_outbox("opencode", 82, telegram_html=html)
        bot = MarkupBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        try:
            self.assertTrue(sender._deliver_one("opencode"))
            self.assertFalse(sender._deliver_one("opencode"))
            self.assertEqual(bot.markups, [None])
        finally:
            sender.close()


if __name__ == "__main__":
    unittest.main()
