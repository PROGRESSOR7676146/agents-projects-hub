import os
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from hermes_codex_router.privacy_scan import (
    _github_signature_verified,
    _metadata_for_privacy_scan,
    scan_text,
)


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

    def test_removes_generated_identity_from_hosted_github_pr_merge(self) -> None:
        metadata = (
            "tree "
            + "a" * 40
            + "\nparent "
            + "b" * 40
            + "\nparent "
            + "c" * 40
            + "\nauthor Private Owner <owner"
            + "@private.invalid> 1 +0000\n"
            + "committer GitHub <noreply"
            + "@github.com> 1 +0000\n\n"
            + "Merge pull request #40 from private-owner/fix\n\n"
            + "Preserve body owner"
            + "@private.invalid\n"
        )
        filtered = _metadata_for_privacy_scan(
            metadata,
            github_signature_verified=True,
            github_owner="private-owner",
        )
        self.assertNotIn("author Private Owner", filtered)
        self.assertNotIn("private-owner/", filtered)
        self.assertIn("Merge pull request #40 from fix", filtered)
        self.assertIn("Preserve body owner" + "@private.invalid", filtered)

    def test_unverified_hosted_merge_is_unchanged(self) -> None:
        metadata = (
            "parent "
            + "b" * 40
            + "\nparent "
            + "c" * 40
            + "\nauthor Private Owner <owner"
            + "@private.invalid> 1 +0000\n"
            + "committer GitHub <noreply"
            + "@github.com> 1 +0000\n\n"
            + "Merge pull request #40 from private-owner/fix\n"
        )
        self.assertEqual(_metadata_for_privacy_scan(metadata), metadata)

    def test_message_headers_do_not_activate_merge_exception(self) -> None:
        metadata = (
            "author Private Owner <owner"
            + "@private.invalid> 1 +0000\n"
            + "committer Contributor <contributors@example.com> 1 +0000\n\n"
            + "parent "
            + "b" * 40
            + "\nparent "
            + "c" * 40
            + "\ncommitter GitHub <noreply"
            + "@github.com> 1 +0000\n"
            + "Merge pull request #40 from private-owner/fix\n"
        )
        self.assertEqual(
            _metadata_for_privacy_scan(
                metadata,
                github_signature_verified=True,
                github_owner="private-owner",
            ),
            metadata,
        )

    def test_verified_hosted_merge_preserves_private_branch_title_and_body(self) -> None:
        metadata = (
            "parent "
            + "b" * 40
            + "\nparent "
            + "c" * 40
            + "\nauthor Private Owner <owner"
            + "@private.invalid> 1 +0000\n"
            + "committer GitHub <noreply"
            + "@github.com> 1 +0000\n\n"
            + "Merge pull request #40 from private-owner/private"
            + "@private.invalid\n\n"
            + "Private title private"
            + "@private.invalid\n\n"
            + "author Body Owner <body"
            + "@private.invalid> 1 +0000\n"
        )
        filtered = _metadata_for_privacy_scan(
            metadata,
            github_signature_verified=True,
            github_owner="private-owner",
        )
        findings = scan_text(Path(".git-metadata/example"), filtered)
        self.assertGreaterEqual(
            sum(item.rule == "non-example email address" for item in findings),
            3,
        )

    def test_verified_hosted_merge_keeps_unmatched_or_invalid_owner(self) -> None:
        for source_owner in ("another-owner", "person" + "@private.invalid"):
            with self.subTest(source_owner=source_owner):
                metadata = (
                    "parent "
                    + "b" * 40
                    + "\nparent "
                    + "c" * 40
                    + "\nauthor GitHub <noreply"
                    + "@github.com> 1 +0000\n"
                    + "committer GitHub <noreply"
                    + "@github.com> 1 +0000\n\n"
                    + f"Merge pull request #40 from {source_owner}/fix\n"
                )
                self.assertEqual(
                    _metadata_for_privacy_scan(
                        metadata,
                        github_signature_verified=True,
                        github_owner="public-owner",
                    ),
                    metadata,
                )

    def test_github_signature_requires_pinned_openpgp_fingerprint(self) -> None:
        valid_status = (
            "[GNUPG:] VALIDSIG 968479A1AFF927E37D1A566BB5690EEEBB952194 "
            "2026-09-13 1 0 4 0 1 8 00 968479A1AFF927E37D1A566BB5690EEEBB952194\n"
        )
        with patch("hermes_codex_router.privacy_scan.subprocess.run") as run:
            run.side_effect = (
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, "", valid_status),
            )
            self.assertTrue(_github_signature_verified(Path("."), "a" * 40))
        verify_argv = run.call_args_list[1].args[0]
        self.assertEqual(verify_argv[0], "/usr/bin/git")
        self.assertIn("gpg.format=openpgp", verify_argv)
        self.assertIn("gpg.program=/usr/bin/gpg", verify_argv)

    def test_github_signature_rejects_alternate_or_missing_validsig(self) -> None:
        for status in (
            "",
            "[GNUPG:] VALIDSIG " + "A" * 40 + " 2026-09-13 1 0 4 0 1 8 00 " + "A" * 40,
        ):
            with self.subTest(status=bool(status)):
                with patch("hermes_codex_router.privacy_scan.subprocess.run") as run:
                    run.side_effect = (
                        subprocess.CompletedProcess([], 0),
                        subprocess.CompletedProcess([], 0, "", status),
                    )
                    self.assertFalse(_github_signature_verified(Path("."), "a" * 40))

    def test_github_signature_import_failure_is_closed(self) -> None:
        with patch("hermes_codex_router.privacy_scan.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 2)
            self.assertFalse(_github_signature_verified(Path("."), "a" * 40))
            self.assertEqual(run.call_count, 1)

    def test_repo_git_verifier_configuration_cannot_authorize_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
            fake = root / "fake-verifier"
            fake.write_text(
                "#!/bin/sh\n"
                "echo '[GNUPG:] NEWSIG'\n"
                "echo '[GNUPG:] GOODSIG B5690EEEBB952194 GitHub'\n"
                "echo '[GNUPG:] VALIDSIG 968479A1AFF927E37D1A566BB5690EEEBB952194 "
                "2026-09-13 1 0 4 0 1 8 00 968479A1AFF927E37D1A566BB5690EEEBB952194'\n"
                "exit 0\n"
            )
            os.chmod(fake, 0o700)
            subprocess.run(
                ["/usr/bin/git", "config", "--local", "gpg.program", str(fake)],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["/usr/bin/git", "config", "--local", "gpg.openpgp.program", str(fake)],
                cwd=root,
                check=True,
            )
            tree = subprocess.run(
                ["/usr/bin/git", "mktree"],
                cwd=root,
                input="",
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            fake_commit = subprocess.run(
                ["/usr/bin/git", "hash-object", "-t", "commit", "-w", "--stdin"],
                cwd=root,
                input=(
                    f"tree {tree}\n"
                    "author Example <account@example.com> 1 +0000\n"
                    "committer Example <account@example.com> 1 +0000\n"
                    "gpgsig -----BEGIN PGP SIGNATURE-----\n"
                    " fake\n"
                    " -----END PGP SIGNATURE-----\n\n"
                    "signed fixture\n"
                ),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            bypass = subprocess.run(
                ["/usr/bin/git", "verify-commit", "--raw", fake_commit],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bypass.returncode, 0)
            subprocess.run(
                ["/usr/bin/git", "config", "--local", "gpg.format", "ssh"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "config",
                    "--local",
                    "gpg.ssh.allowedSignersFile",
                    str(fake),
                ],
                cwd=root,
                check=True,
            )
            self.assertFalse(_github_signature_verified(root, fake_commit))

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
