from __future__ import annotations

import unittest

from hermes_codex_router.telegram_multipart import send_telegram_html_parts, split_telegram_html


class TelegramMultipartTests(unittest.TestCase):
    def test_short_html_remains_one_part(self) -> None:
        self.assertEqual(split_telegram_html("Hello &amp; goodbye"), ("Hello &amp; goodbye",))

    def test_long_plain_text_is_ordered_and_bounded(self) -> None:
        source = " ".join(f"word-{index}" for index in range(1800))
        parts = split_telegram_html(source, limit=500)
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(0 < len(part) <= 500 for part in parts))
        self.assertEqual("".join(parts), source)

    def test_balances_expandable_blockquote_across_parts(self) -> None:
        source = (
            "Answer\n\n<blockquote expandable>" + ("status &amp; detail\n" * 100) + "</blockquote>"
        )
        parts = split_telegram_html(source, limit=240)
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(part) <= 240 for part in parts))
        for part in parts[1:]:
            self.assertTrue(part.startswith("<blockquote expandable>"))
        for part in parts:
            if "<blockquote" in part:
                self.assertTrue(part.endswith("</blockquote>"))

    def test_never_cuts_html_entity(self) -> None:
        parts = split_telegram_html("x &lt; y " * 100, limit=80)
        self.assertTrue(all(part.count("&lt;") == part.count("&") for part in parts))

    def test_immediate_sender_publishes_every_part_in_order(self) -> None:
        class Sender:
            def __init__(self) -> None:
                self.sent: list[str] = []

            def send_html(self, chat_id: int, thread_id: int, html: str) -> int:
                self.assertions = (chat_id, thread_id)
                self.sent.append(html)
                return len(self.sent)

        sender = Sender()
        ids = send_telegram_html_parts(sender, 42, 1, "message " * 1000)
        self.assertGreater(len(ids), 1)
        self.assertEqual(ids, tuple(range(1, len(ids) + 1)))
        self.assertEqual("".join(sender.sent), ("message " * 1000).strip())


if __name__ == "__main__":
    unittest.main()
