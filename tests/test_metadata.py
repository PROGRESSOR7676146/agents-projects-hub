from __future__ import annotations

import unittest

from hermes_codex_router.codex_appserver import LimitWindow, RateLimits, TurnResult
from hermes_codex_router.metadata import format_agent_response, format_telegram_response


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
            session_label="Example Project Alpha · Backend · Codex",
            limits=limits,
            timezone_name="Europe/Moscow",
        )
        self.assertIn("Fixed &lt;main&gt; &amp; tests", rendered)
        self.assertIn("<blockquote expandable>", rendered)
        self.assertIn(
            "Session: Example Project Alpha · Backend / Agent: Codex · gpt-5.6-sol-high",
            rendered,
        )
        self.assertIn("Context remaining: 75.0%", rendered)
        self.assertIn("5-hour remaining: 65%, reset: Feb. 2, 05:40", rendered)
        self.assertIn("Weekly remaining: 48%, reset: Feb. 8, 00:33", rendered)
        self.assertNotIn("2026", rendered)
        self.assertNotIn("MSK", rendered)
        self.assertNotIn("Model:", rendered)
        self.assertNotIn("Effort:", rendered)

    def test_external_footer_is_compact_and_omits_unavailable_fields(self) -> None:
        rendered = format_agent_response(
            "Done",
            {
                "Session": "Hub · General · Antigravity",
                "Agent": "Antigravity",
                "Runtime": "antigravity",
                "Model": "gemini-3.7-flash",
                "Effort": "high",
                "Context remaining": "unavailable",
                "Usage windows": "unavailable",
            },
        )

        self.assertIn(
            "Session: Hub · General / Agent: Antigravity · gemini-3.7-flash-high",
            rendered,
        )
        self.assertNotIn("Runtime", rendered)
        self.assertNotIn("unavailable", rendered)
        self.assertEqual(rendered.count("\n", rendered.index("<blockquote")), 0)


if __name__ == "__main__":
    unittest.main()
