from __future__ import annotations

import unittest

from hermes_codex_router.codex_appserver import LimitWindow, RateLimits, TurnResult
from hermes_codex_router.metadata import format_telegram_response


class MetadataTests(unittest.TestCase):
    def test_formats_escaped_answer_and_collapsed_exact_session_details(self) -> None:
        result = TurnResult(
            text="Fixed <main> & tests",
            context_window=100000,
            context_tokens_used=25000,
        )
        limits = RateLimits(
            primary=LimitWindow(remaining_percent=65, resets_at=1770000000, duration_minutes=300),
            secondary=LimitWindow(
                remaining_percent=48, resets_at=1770500000, duration_minutes=10080
            ),
        )
        rendered = format_telegram_response(
            result=result,
            agent="Codex",
            model="gpt-5.6-sol",
            effort="high",
            session_label="Pythia · Backend · Codex",
            limits=limits,
            timezone_name="Europe/Moscow",
        )
        self.assertIn("Fixed &lt;main&gt; &amp; tests", rendered)
        self.assertIn("<blockquote expandable>", rendered)
        self.assertIn("Context remaining: 75.0%", rendered)
        self.assertIn("5-hour remaining: 65%", rendered)
        self.assertIn("Weekly remaining: 48%", rendered)
        self.assertIn("Model: gpt-5.6-sol", rendered)
        self.assertIn("Effort: high", rendered)


if __name__ == "__main__":
    unittest.main()
