from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.privacy_scan import (
    _git_object_bytes_match_oid,
    _is_github_merge_candidate,
    _read_git_object,
    _read_public_author_email,
    _scan_metadata_for_privacy,
    scan_history,
    scan_text,
)

ALLOWED_EMAIL = "owner@public." + "example.invalid"
ROOT = Path(__file__).resolve().parents[1]


def hosted_metadata(
    *,
    author_email: str = ALLOWED_EMAIL,
    author_name: str = "Example Owner",
    owner: str = "example-owner",
    extra_body: str = "",
    author_timestamp: str = "1",
    author_timezone: str = "+0000",
    extra_headers: str = "",
) -> bytes:
    return (
        "tree "
        + "a" * 40
        + "\nparent "
        + "b" * 40
        + "\nparent "
        + "c" * 40
        + f"\nauthor {author_name} <{author_email}> {author_timestamp} {author_timezone}\n"
        + extra_headers
        + "committer GitHub <noreply@github.com> 1 +0000\n\n"
        + f"Merge pull request #1 from {owner}/fix\n"
        + extra_body
    ).encode()


def synthetic_metadata(
    *,
    first_parent: str = "b" * 40,
    second_parent: str = "c" * 40,
    message_head: str | None = None,
    message_base: str | None = None,
    author_email: str = ALLOWED_EMAIL,
    author_name: str = "Example Owner",
) -> bytes:
    return (
        "tree "
        + "a" * 40
        + f"\nparent {first_parent}\nparent {second_parent}"
        + f"\nauthor {author_name} <{author_email}> 1 +0000\n"
        + "committer GitHub <noreply@github.com> 1 +0000\n\n"
        + f"Merge {message_head or second_parent} into {message_base or first_parent}\n"
    ).encode()


class PublicAuthorPolicyTests(unittest.TestCase):
    def scan(
        self,
        metadata: bytes,
        *,
        declared: bytes | None = ALLOWED_EMAIL.encode(),
        signature_verified: bool = True,
        owner: str | None = "example-owner",
    ) -> set[str]:
        return {
            finding.rule
            for finding in _scan_metadata_for_privacy(
                Path(".git-metadata/example"),
                metadata,
                declared_author_email=declared,
                github_signature_verified=signature_verified,
                github_owner=owner,
            )
        }

    def scan_all(
        self,
        metadata: bytes,
        *,
        declared: bytes | None = ALLOWED_EMAIL.encode(),
        signature_verified: bool = True,
        owner: str | None = "example-owner",
    ) -> list[str]:
        return [
            finding.rule
            for finding in _scan_metadata_for_privacy(
                Path(".git-metadata/example"),
                metadata,
                declared_author_email=declared,
                github_signature_verified=signature_verified,
                github_owner=owner,
            )
        ]

    def test_author_exception_keeps_non_identity_findings(self) -> None:
        for author_name, rule in (
            ("ghp_" + "FictionalTokenValue" * 3, "credential-like high-entropy value"),
            ("/home/" + "fictional-private/project", "owner-specific home path"),
            ("https://t.me/" + "+fictional-invite", "private Telegram invite link"),
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.scan(hosted_metadata(author_name=author_name)))

    def test_only_exact_structural_author_email_is_exempt(self) -> None:
        body = f"\nRepeated {ALLOWED_EMAIL}\n"
        rules = self.scan_all(hosted_metadata(extra_body=body))
        self.assertEqual(rules.count("non-example email address"), 1)

    def test_identical_email_in_display_name_is_not_exempt(self) -> None:
        rules = self.scan_all(hosted_metadata(author_name=f"Example {ALLOWED_EMAIL}"))
        self.assertEqual(rules.count("non-example email address"), 1)

    def test_byte_offsets_remain_exact_after_unicode_display_name(self) -> None:
        rules = self.scan_all(
            hosted_metadata(
                author_name="Fictional \N{SNOWMAN}",
                extra_body=f"\nRepeated {ALLOWED_EMAIL}\n",
            )
        )
        self.assertEqual(rules.count("non-example email address"), 1)

    def test_absent_declaration_preserves_email_finding(self) -> None:
        self.assertIn(
            "non-example email address",
            self.scan(hosted_metadata(), declared=None),
        )

    def test_case_or_substring_does_not_match_declaration(self) -> None:
        for candidate in (ALLOWED_EMAIL.upper(), "x" + ALLOWED_EMAIL):
            with self.subTest(candidate=candidate):
                self.assertIn(
                    "non-example email address",
                    self.scan(hosted_metadata(author_email=candidate)),
                )

    def test_other_rule_on_declared_email_bytes_is_not_exempt(self) -> None:
        credential_email = "ghp_" + "A" * 35 + "@public.example.invalid"
        self.assertIn(
            "credential-like high-entropy value",
            self.scan(
                hosted_metadata(author_email=credential_email),
                declared=credential_email.encode(),
            ),
        )

    def test_email_fingerprint_is_exempt_only_on_same_absolute_span(self) -> None:
        digest = hashlib.sha256(ALLOWED_EMAIL.casefold().encode()).hexdigest()
        with patch(
            "hermes_codex_router.privacy_scan._PRIVATE_FINGERPRINTS",
            frozenset({digest}),
        ):
            self.assertEqual(self.scan(hosted_metadata()), set())
            self.assertEqual(
                self.scan(hosted_metadata(extra_body=f"\n{ALLOWED_EMAIL}\n")),
                {"non-example email address", "private deployment fingerprint"},
            )

    def test_origin_owner_exemption_is_exact_and_rule_specific(self) -> None:
        owner = "example-owner"
        digest = hashlib.sha256(owner.casefold().encode()).hexdigest()
        metadata = hosted_metadata(
            author_name=owner,
            extra_body=f"\nRepeated {owner}\n",
        )
        with (
            patch(
                "hermes_codex_router.privacy_scan._PRIVATE_FINGERPRINTS",
                frozenset({digest}),
            ),
            patch(
                "hermes_codex_router.privacy_scan._INVITE_RE",
                re.compile(owner),
            ),
        ):
            rules = self.scan_all(metadata)
        self.assertEqual(rules.count("private deployment fingerprint"), 1)
        self.assertEqual(rules.count("private Telegram invite link"), 3)

    def test_synthetic_author_name_may_repeat_exact_origin_owner(self) -> None:
        owner = "example-owner"
        digest = hashlib.sha256(owner.casefold().encode()).hexdigest()
        with patch(
            "hermes_codex_router.privacy_scan._PRIVATE_FINGERPRINTS",
            frozenset({digest}),
        ):
            self.assertNotIn(
                "private deployment fingerprint",
                self.scan(synthetic_metadata(author_name=owner)),
            )

    def test_author_name_origin_owner_match_is_byte_exact(self) -> None:
        owner = "example-owner"
        cases = (
            "EXAMPLE-owner",
            "example-owner-extra",
            "prefix-example-owner",
            "example-owner ",
            "ex\N{CYRILLIC SMALL LETTER A}mple-owner",
            "Prefix example-owner",
        )
        for author_name in cases:
            token = owner if " " in author_name else author_name
            digest = hashlib.sha256(token.casefold().encode()).hexdigest()
            with (
                self.subTest(author_name=author_name),
                patch(
                    "hermes_codex_router.privacy_scan._PRIVATE_FINGERPRINTS",
                    frozenset({digest}),
                ),
            ):
                self.assertIn(
                    "private deployment fingerprint",
                    self.scan(hosted_metadata(author_name=author_name)),
                )

    def test_author_name_rule_requires_matching_hosted_source_owner(self) -> None:
        owner = "example-owner"
        digest = hashlib.sha256(owner.casefold().encode()).hexdigest()
        with patch(
            "hermes_codex_router.privacy_scan._PRIVATE_FINGERPRINTS",
            frozenset({digest}),
        ):
            self.assertIn(
                "private deployment fingerprint",
                self.scan(
                    hosted_metadata(author_name=owner, owner="another-owner"),
                    owner=owner,
                ),
            )

    def test_synthetic_author_name_rule_requires_valid_origin_owner(self) -> None:
        owner = "example-owner"
        digest = hashlib.sha256(owner.casefold().encode()).hexdigest()
        for configured_owner in (None, "-example-owner", "example--owner", "example-owner/"):
            with (
                self.subTest(configured_owner=configured_owner),
                patch(
                    "hermes_codex_router.privacy_scan._PRIVATE_FINGERPRINTS",
                    frozenset({digest}),
                ),
            ):
                self.assertIn(
                    "private deployment fingerprint",
                    self.scan(
                        synthetic_metadata(author_name=owner),
                        owner=configured_owner,
                    ),
                )

    def test_malformed_or_duplicate_author_headers_have_no_exception(self) -> None:
        duplicate = f"author Other <{ALLOWED_EMAIL}> 1 +0000\n"
        for metadata in (
            hosted_metadata(author_timestamp="bad"),
            hosted_metadata(author_timezone="0000"),
            hosted_metadata(author_timezone="+2460"),
            hosted_metadata(extra_headers=duplicate),
            hosted_metadata(extra_headers="author malformed\n"),
            hosted_metadata(extra_headers="parent malformed\n"),
            hosted_metadata(extra_headers="tree malformed\n"),
            hosted_metadata().replace(
                ("tree " + "a" * 40 + "\n").encode(),
                ("tree " + "a" * 40 + "\ntree " + "d" * 40 + "\n").encode(),
                1,
            ),
            hosted_metadata(extra_headers="parent " + "d" * 40 + "\n"),
            hosted_metadata(extra_headers="committer GitHub <noreply@github.com> 2 +0000\n"),
            hosted_metadata(extra_headers="committer GitHub <noreply@github.com> 2 +2460\n"),
        ):
            with self.subTest(metadata=metadata[:20]):
                self.assertIn("non-example email address", self.scan(metadata))

    def test_context_must_be_verified_and_owner_must_match(self) -> None:
        self.assertIn(
            "non-example email address",
            self.scan(hosted_metadata(), signature_verified=False),
        )
        self.assertIn(
            "non-example email address",
            self.scan(hosted_metadata(), owner="another-owner"),
        )
        self.assertIn(
            "non-example email address",
            self.scan(hosted_metadata(), owner="EXAMPLE-owner"),
        )

    def test_crlf_nul_and_extra_structural_data_have_no_exception(self) -> None:
        for metadata in (
            hosted_metadata().replace(b"\n", b"\r\n"),
            hosted_metadata() + b"\x00",
        ):
            with self.subTest(suffix=metadata[-2:]):
                self.assertIn("non-example email address", self.scan(metadata))

    def test_email_is_not_exempt_in_committer_body_trailer_or_file(self) -> None:
        for text in (
            f"committer Example <{ALLOWED_EMAIL}> 1 +0000",
            f"Body {ALLOWED_EMAIL}",
            f"Signed-off-by: Example <{ALLOWED_EMAIL}>",
        ):
            with self.subTest(prefix=text.split()[0]):
                self.assertIn(
                    "non-example email address",
                    {finding.rule for finding in scan_text(Path("fixture"), text)},
                )

    def test_signed_synthetic_requires_exact_parent_order(self) -> None:
        valid = synthetic_metadata()
        reversed_order = synthetic_metadata(
            message_head="b" * 40,
            message_base="c" * 40,
        )
        self.assertNotIn("non-example email address", self.scan(valid))
        self.assertIn("non-example email address", self.scan(reversed_order))
        self.assertTrue(_is_github_merge_candidate(valid.decode()))
        self.assertFalse(_is_github_merge_candidate(reversed_order.decode()))

    def test_unsigned_synthetic_has_no_exception(self) -> None:
        self.assertIn(
            "non-example email address",
            self.scan(synthetic_metadata(), signature_verified=False),
        )


class PublicAuthorDeclarationTests(unittest.TestCase):
    def read(self, data: bytes, *, mode: int = 0o600) -> bytes | None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "declaration"
            path.write_bytes(data)
            path.chmod(mode)
            with patch.dict(
                os.environ,
                {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(path)},
                clear=False,
            ):
                return _read_public_author_email(ROOT)

    def test_accepts_exact_email_with_zero_or_one_final_lf(self) -> None:
        self.assertEqual(self.read(ALLOWED_EMAIL.encode()), ALLOWED_EMAIL.encode())
        self.assertEqual(self.read((ALLOWED_EMAIL + "\n").encode()), ALLOWED_EMAIL.encode())

    def test_rejects_invalid_content_without_output(self) -> None:
        cases = (
            (ALLOWED_EMAIL + "\r\n").encode(),
            (ALLOWED_EMAIL + "\n\n").encode(),
            (ALLOWED_EMAIL + "\x00").encode(),
            ("оwner@public." + "example.invalid").encode(),
            (ALLOWED_EMAIL + "\nother@" + "example.invalid").encode(),
            b"x" * 1024,
        )
        for data in cases:
            with self.subTest(size=len(data)):
                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertIsNone(self.read(data))
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_rejects_relative_inside_checkout_and_bad_mode(self) -> None:
        with patch.dict(
            os.environ,
            {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": "relative-policy"},
            clear=False,
        ):
            self.assertIsNone(_read_public_author_email(ROOT))
        self.assertIsNone(self.read(ALLOWED_EMAIL.encode(), mode=0o640))

    def test_rejects_file_inside_checkout_even_when_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "ignored-policy"
            path.write_bytes(ALLOWED_EMAIL.encode())
            path.chmod(0o600)
            with patch.dict(
                os.environ,
                {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(path)},
                clear=False,
            ):
                self.assertIsNone(_read_public_author_email(root))

    def test_rejects_symlink_hardlink_fifo_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(ALLOWED_EMAIL.encode())
            source.chmod(0o600)
            candidates = (root / "link", root / "hard", root / "fifo", root / "folder")
            candidates[0].symlink_to(source)
            os.link(source, candidates[1])
            os.mkfifo(candidates[2], 0o600)
            candidates[3].mkdir(mode=0o600)
            for path in candidates:
                with self.subTest(kind=path.name):
                    with patch.dict(
                        os.environ,
                        {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(path)},
                        clear=False,
                    ):
                        self.assertIsNone(_read_public_author_email(ROOT))

    def test_rejects_wrong_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "declaration"
            path.write_bytes(ALLOWED_EMAIL.encode())
            path.chmod(0o600)
            with (
                patch.dict(
                    os.environ,
                    {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(path)},
                    clear=False,
                ),
                patch("hermes_codex_router.privacy_scan.os.getuid", return_value=os.getuid() + 1),
            ):
                self.assertIsNone(_read_public_author_email(ROOT))

    def test_valid_declaration_is_silent(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(self.read(ALLOWED_EMAIL.encode()), ALLOWED_EMAIL.encode())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_reader_error_is_silent_and_discloses_no_value_or_path(self) -> None:
        private_path = "/tmp/fictional-private-policy"
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.dict(
                os.environ,
                {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": private_path},
                clear=False,
            ),
            patch(
                "hermes_codex_router.privacy_scan.os.open",
                side_effect=ValueError("fictional internal failure"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertIsNone(_read_public_author_email(ROOT))
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(combined, "")
        self.assertNotIn(ALLOWED_EMAIL, combined)
        self.assertNotIn(private_path, combined)


class GitObjectReadTests(unittest.TestCase):
    def test_read_ignores_real_git_replace_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            root = Path(directory_name)

            def git(*args: str, input_data: bytes | None = None) -> bytes:
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    input=input_data,
                    capture_output=True,
                    check=True,
                ).stdout

            git("init", "-q")
            original = git("hash-object", "-w", "--stdin", input_data=b"original\n").strip()
            replacement = git("hash-object", "-w", "--stdin", input_data=b"replacement\n").strip()
            git("replace", original.decode(), replacement.decode())
            self.assertEqual(git("cat-file", "-p", original.decode()), b"replacement\n")
            self.assertEqual(_read_git_object(root, original.decode()), b"original\n")
            self.assertTrue(_git_object_bytes_match_oid("blob", original.decode(), b"original\n"))
            self.assertFalse(
                _git_object_bytes_match_oid("blob", original.decode(), b"replacement\n")
            )

    def test_canonical_history_scan_uses_fictional_external_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            container = Path(directory_name)
            root = container / "repository"
            root.mkdir()

            def git(
                *args: str,
                input_data: bytes | None = None,
                environment: dict[str, str] | None = None,
            ) -> str:
                return (
                    subprocess.run(
                        ["git", *args],
                        cwd=root,
                        env=environment,
                        input=input_data,
                        capture_output=True,
                        text=False,
                        check=True,
                    )
                    .stdout.decode()
                    .strip()
                )

            git("init", "-q")
            tree = git("mktree", input_data=b"")
            ordinary_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Example Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Example Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
            }
            first_parent = git(
                "commit-tree",
                tree,
                input_data=b"Example base\n",
                environment=ordinary_environment,
            )
            second_parent = git(
                "commit-tree",
                tree,
                input_data=b"Example head\n",
                environment=ordinary_environment,
            )
            merge_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "example-owner",
                "GIT_AUTHOR_EMAIL": ALLOWED_EMAIL,
                "GIT_COMMITTER_NAME": "GitHub",
                "GIT_COMMITTER_EMAIL": "noreply@github.com",
            }
            merge = git(
                "commit-tree",
                tree,
                "-p",
                first_parent,
                "-p",
                second_parent,
                input_data=b"Merge pull request #1 from example-owner/fix\n",
                environment=merge_environment,
            )
            git("update-ref", "refs/heads/main", merge)
            git("remote", "add", "origin", "https://github.com/example-owner/example.git")
            declaration = container / "public-author-policy"
            declaration.write_bytes(ALLOWED_EMAIL.encode())
            declaration.chmod(0o600)
            with (
                patch.dict(
                    os.environ,
                    {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(declaration)},
                    clear=False,
                ),
                patch(
                    "hermes_codex_router.privacy_scan._github_signature_verified",
                    return_value=True,
                ) as verifier,
            ):
                self.assertEqual(scan_history(root), [])
            verifier.assert_called_once_with(root, merge)
