from __future__ import annotations

import unittest

from hermes_codex_router.telegram_interaction import telegram_turn_prompt


class TelegramInteractionPromptTests(unittest.TestCase):
    def test_new_session_receives_full_contract_and_provider_note(self) -> None:
        prompt = telegram_turn_prompt("Inspect the repository.", runtime="codex", new_session=True)

        self.assertIn("TELEGRAM INTERACTION CONTRACT v1", prompt)
        self.assertIn("Telegram", prompt)
        self.assertIn("commentary", prompt)
        self.assertIn("Do not switch into a provider-specific plan-only mode", prompt)
        self.assertIn("CURRENT USER TURN:\nInspect the repository.", prompt)

    def test_existing_session_receives_compact_transport_reminder(self) -> None:
        prompt = telegram_turn_prompt("Continue.", runtime="opencode", new_session=False)

        self.assertIn("TELEGRAM TRANSPORT REMINDER v1", prompt)
        self.assertNotIn("TELEGRAM INTERACTION CONTRACT v1", prompt)
        self.assertIn("CURRENT USER TURN:\nContinue.", prompt)

    def test_contract_does_not_claim_unavailable_ui_actions(self) -> None:
        prompt = telegram_turn_prompt("Create a report.", runtime="antigravity", new_session=True)

        self.assertIn("Never claim that a file, button, or reaction was sent", prompt)
        self.assertIn("Do not expose hidden reasoning", prompt)

    def test_rejects_empty_user_turn(self) -> None:
        with self.assertRaises(ValueError):
            telegram_turn_prompt("   ", runtime="codex", new_session=True)


if __name__ == "__main__":
    unittest.main()
