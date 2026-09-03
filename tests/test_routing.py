from __future__ import annotations

import unittest

from hermes_codex_router.routing import (
    Command,
    decide_targets,
    is_emergency_stop,
    parse_command,
    parse_context_request,
)


class RoutingTests(unittest.TestCase):
    usernames = {
        "codex": "project_codex_bot",
        "gemini": "example_gemini_bot",
        "hermes": "project_hermes_bot",
    }

    def test_plain_message_targets_only_active_agent(self) -> None:
        self.assertEqual(
            decide_targets("check the tests", active_agent="codex", usernames=self.usernames),
            ("codex",),
        )

    def test_mention_targets_satellite_without_active_agent(self) -> None:
        self.assertEqual(
            decide_targets(
                "@example_gemini_bot review this",
                active_agent="codex",
                usernames=self.usernames,
            ),
            ("gemini",),
        )

    def test_multiple_mentions_target_each_agent_once_in_text_order(self) -> None:
        self.assertEqual(
            decide_targets(
                "@example_gemini_bot ask @project_hermes_bot and @example_gemini_bot",
                active_agent="codex",
                usernames=self.usernames,
            ),
            ("gemini", "hermes"),
        )

    def test_unknown_mention_does_not_suppress_active_agent(self) -> None:
        self.assertEqual(
            decide_targets("ask @someone_else", active_agent="codex", usernames=self.usernames),
            ("codex",),
        )

    def test_reply_to_known_bot_targets_only_the_reply_author(self) -> None:
        self.assertEqual(
            decide_targets(
                "relax, this is only a connection test",
                active_agent="codex",
                usernames=self.usernames,
                reply_to_username="example_gemini_bot",
            ),
            ("gemini",),
        )

    def test_reply_author_has_priority_over_mentions_and_active_agent(self) -> None:
        self.assertEqual(
            decide_targets(
                "@project_codex_bot do not intercept this reply",
                active_agent="codex",
                usernames=self.usernames,
                reply_to_username="example_gemini_bot",
            ),
            ("gemini",),
        )

    def test_textual_quote_without_telegram_reply_stays_with_active_agent(self) -> None:
        self.assertEqual(
            decide_targets(
                "> quoted bot text\nrelax",
                active_agent="codex",
                usernames=self.usernames,
                reply_to_username=None,
            ),
            ("codex",),
        )

    def test_commands_are_parsed_without_accepting_paths(self) -> None:
        self.assertEqual(parse_command("/new"), Command("new", ()))
        self.assertEqual(parse_command("/agent Gemini"), Command("agent", ("gemini",)))
        self.assertEqual(
            parse_command("/model gpt-5.6-sol high"),
            Command("model", ("gpt-5.6-sol", "high")),
        )
        self.assertIsNone(
            parse_command("please inspect /home/example/projects/Example Project Alpha")
        )

    def test_emergency_stop_matches_only_the_whole_utterance(self) -> None:
        for value in ("/stop", "/STOP@example_hub_bot", " stop! ", "HALT", "СтОп", "стой"):
            self.assertTrue(is_emergency_stop(value), value)
        for value in ("не останавливайся", "stop after tests", "потом стоп", "/status"):
            self.assertFalse(is_emergency_stop(value), value)

    def test_explicit_context_request_is_bounded_and_deterministic(self) -> None:
        self.assertEqual(parse_context_request("/context"), (None, 8))
        self.assertEqual(
            parse_context_request("/context@example_hub_bot Antigravity 20"),
            ("antigravity", 20),
        )
        self.assertIsNone(parse_context_request("/context antigravity 21"))
        self.assertIsNone(parse_context_request("please include context"))


if __name__ == "__main__":
    unittest.main()
