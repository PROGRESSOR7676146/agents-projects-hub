from __future__ import annotations

import unittest

from hermes_codex_router.telegram_interaction import (
    TLIVE_APPROVAL_ONLY_MARKER,
    telegram_contract_version,
    telegram_developer_instructions,
    telegram_turn_prompt,
    telegram_user_turn_prompt,
)


class TelegramInteractionPromptTests(unittest.TestCase):
    def test_v2_rollout_is_codex_only(self) -> None:
        self.assertEqual(telegram_contract_version("codex"), 2)
        self.assertEqual(telegram_contract_version("opencode"), 1)
        self.assertEqual(telegram_contract_version("antigravity"), 1)
        self.assertEqual(telegram_contract_version("hermes"), 1)

    def test_new_session_receives_full_contract_and_provider_note(self) -> None:
        prompt = telegram_developer_instructions(runtime="codex", new_session=True)

        self.assertIn("TELEGRAM INTERACTION CONTRACT v2", prompt)
        self.assertIn("Telegram", prompt)
        self.assertIn("commentary", prompt)
        self.assertIn("Do not switch into a provider-specific plan-only mode", prompt)
        self.assertIn("without an artificial delay", prompt)

    def test_existing_session_receives_compact_transport_reminder(self) -> None:
        prompt = telegram_developer_instructions(runtime="opencode", new_session=False)

        self.assertIn("TELEGRAM TRANSPORT REMINDER v1", prompt)
        self.assertNotIn("TELEGRAM INTERACTION CONTRACT v1", prompt)

    def test_contract_does_not_claim_unavailable_ui_actions(self) -> None:
        prompt = telegram_turn_prompt("Create a report.", runtime="antigravity", new_session=True)

        self.assertIn("Never claim that a file, button, or reaction was sent", prompt)
        self.assertIn("Do not expose hidden reasoning", prompt)

    def test_turn_can_name_one_exact_artifact_staging_directory(self) -> None:
        prompt = telegram_user_turn_prompt(
            "Create a report.",
            staging_dir="/home/example/project/.hub/staging/example-job",
        )
        self.assertIn("/home/example/project/.hub/staging/example-job", prompt)
        self.assertIn("Files elsewhere are not attached", prompt)
        self.assertNotIn("TELEGRAM INTERACTION CONTRACT", prompt)
        self.assertNotIn("TELEGRAM TRANSPORT REMINDER", prompt)

    def test_codex_user_turn_marks_tlive_as_approval_only(self) -> None:
        prompt = telegram_user_turn_prompt("Do the work.")

        self.assertTrue(prompt.startswith(f"{TLIVE_APPROVAL_ONLY_MARKER}\n\n"))
        self.assertIn("CURRENT USER TURN:\nDo the work.", prompt)

    def test_fallback_prompt_keeps_contract_for_non_native_runtimes(self) -> None:
        prompt = telegram_turn_prompt("Continue.", runtime="opencode", new_session=False)

        self.assertIn("TELEGRAM TRANSPORT REMINDER v1", prompt)
        self.assertIn("CURRENT USER TURN:\nContinue.", prompt)

    def test_rejects_empty_user_turn(self) -> None:
        with self.assertRaises(ValueError):
            telegram_turn_prompt("   ", runtime="codex", new_session=True)


if __name__ == "__main__":
    unittest.main()
