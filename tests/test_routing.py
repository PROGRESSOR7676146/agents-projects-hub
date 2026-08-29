from __future__ import annotations

import unittest

from hermes_codex_router.routing import Command, decide_targets, parse_command


class RoutingTests(unittest.TestCase):
    usernames = {
        "codex": "project_codex_bot",
        "gemini": "pythia_gemini_bot",
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
                "@pythia_gemini_bot review this",
                active_agent="codex",
                usernames=self.usernames,
            ),
            ("gemini",),
        )

    def test_multiple_mentions_target_each_agent_once_in_text_order(self) -> None:
        self.assertEqual(
            decide_targets(
                "@pythia_gemini_bot ask @project_hermes_bot and @pythia_gemini_bot",
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

    def test_commands_are_parsed_without_accepting_paths(self) -> None:
        self.assertEqual(parse_command("/new"), Command("new", ()))
        self.assertEqual(parse_command("/new all"), Command("new", ("all",)))
        self.assertEqual(parse_command("/agent Gemini"), Command("agent", ("gemini",)))
        self.assertEqual(
            parse_command("/model gpt-5.6-sol high"),
            Command("model", ("gpt-5.6-sol", "high")),
        )
        self.assertIsNone(parse_command("please inspect /home/tester/projects/Pythia"))


if __name__ == "__main__":
    unittest.main()
