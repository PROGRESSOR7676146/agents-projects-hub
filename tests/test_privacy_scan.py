from pathlib import Path
from unittest import TestCase

from hermes_codex_router.privacy_scan import _metadata_for_privacy_scan, scan_text


class PrivacyScanTests(TestCase):
    def test_ignores_only_author_of_github_synthetic_pr_merge(self) -> None:
        metadata = (
            "tree " + "a" * 40 + "\n"
            "parent " + "b" * 40 + "\n"
            "parent " + "c" * 40 + "\n"
            "author Private Owner <owner" + "@private.invalid> 1 +0000\n"
            "committer GitHub <noreply" + "@github.com> 1 +0000\n\n"
            "Merge " + "d" * 40 + " into " + "e" * 40 + "\n"
        )
        filtered = _metadata_for_privacy_scan(metadata)
        self.assertNotIn("owner" + "@private.invalid", filtered)
        self.assertIn("committer GitHub", filtered)

    def test_does_not_ignore_author_of_an_ordinary_merge(self) -> None:
        metadata = (
            "parent " + "b" * 40 + "\n"
            "parent " + "c" * 40 + "\n"
            "author Private Owner <owner" + "@private.invalid> 1 +0000\n"
            "committer Contributor <contributors@example.com> 1 +0000\n\n"
            "Merge a feature branch\n"
        )
        self.assertEqual(_metadata_for_privacy_scan(metadata), metadata)

    def assert_rule(self, text: str, rule: str) -> None:
        findings = scan_text(Path("fixture.txt"), text)
        self.assertIn(rule, {finding.rule for finding in findings})

    def test_rejects_non_example_email(self) -> None:
        self.assert_rule("person" + "@private.invalid", "non-example email address")

    def test_rejects_owner_home_path(self) -> None:
        self.assert_rule("/home/" + "private-user/project", "owner-specific home path")

    def test_rejects_private_invite(self) -> None:
        self.assert_rule("https://t.me/" + "+private-code", "private Telegram invite link")

    def test_rejects_credential_like_value(self) -> None:
        value = "1234567890AA" + "abcdefghijklmnopqrstuvwxyz012345"
        self.assert_rule(value, "credential-like high-entropy value")

    def test_rejects_non_placeholder_chat_id(self) -> None:
        self.assert_rule("-100" + "7654321098", "non-placeholder Telegram chat ID")

    def test_rejects_non_example_bot_username(self) -> None:
        self.assert_rule("@private_" + "service_bot", "non-example Telegram bot username")

    def test_rejects_non_placeholder_session_uuid(self) -> None:
        value = "12345678-1234-4234-8234-" + "123456789abc"
        self.assert_rule(value, "non-placeholder session UUID")

    def test_rejects_raw_session_marker(self) -> None:
        self.assert_rule("<environment" + "_context>", "raw agent/session transcript marker")

    def test_allows_publishable_examples(self) -> None:
        text = "account@example.com /home/example/project @example_agent_bot -1001234567890"
        self.assertEqual(scan_text(Path("fixture.txt"), text), [])
