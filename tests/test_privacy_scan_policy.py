from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.privacy_scan import (
    _git_object_bytes_match_oid,
    _read_git_object,
    _scan_metadata_for_privacy,
    scan_history,
)

PERSON_EMAIL = "person@private." + "example.invalid"
PRIVATE_TOKEN = "private-" + "project"
METADATA = Path(".git-metadata")
HOME = "/ho" + "me/"


def commit_metadata(
    *,
    author: str = f"Example Person <{PERSON_EMAIL}>",
    committer: str = "GitHub <noreply@github.com>",
    message: str = "Example change\n",
) -> bytes:
    return (
        "tree "
        + "a" * 40
        + f"\nauthor {author} 1 +0000\ncommitter {committer} 1 +0000\n\n"
        + message
    ).encode()


def rules(metadata: bytes) -> list[str]:
    return [finding.rule for finding in _scan_metadata_for_privacy(METADATA, metadata)]


def fingerprint(*tokens: str):
    digests = frozenset(hashlib.sha256(token.casefold().encode()).hexdigest() for token in tokens)
    return patch("hermes_codex_router.privacy_scan._PRIVATE_FINGERPRINTS", digests)


class CommitAuthorshipTests(unittest.TestCase):
    def test_author_and_committer_identities_are_publishable(self) -> None:
        metadata = commit_metadata(committer=f"Example Person <{PERSON_EMAIL}>")
        self.assertEqual(rules(metadata), [])

    def test_co_author_and_sign_off_trailers_are_publishable(self) -> None:
        message = (
            "Example change\n\n"
            f"Co-Authored-By: Example Assistant <{PERSON_EMAIL}>\n"
            f"Signed-off-by: Example Person <{PERSON_EMAIL}>\n"
        )
        self.assertEqual(rules(commit_metadata(message=message)), [])

    def test_identity_fingerprints_are_publishable(self) -> None:
        with fingerprint(PRIVATE_TOKEN, PERSON_EMAIL):
            metadata = commit_metadata(
                author=f"{PRIVATE_TOKEN} <{PERSON_EMAIL}>",
                message=f"Merge pull request #1 from {PRIVATE_TOKEN}/fix\n",
            )
            self.assertEqual(rules(metadata), [])

    def test_message_prose_is_still_scanned(self) -> None:
        with fingerprint(PRIVATE_TOKEN):
            metadata = commit_metadata(
                message=(
                    f"Fix {PRIVATE_TOKEN} routing, write to {PERSON_EMAIL}\n"
                    f"Merge pull request #1 from example/{PRIVATE_TOKEN}\n"
                )
            )
            self.assertEqual(
                sorted(rules(metadata)),
                [
                    "non-example email address",
                    "private deployment fingerprint",
                    "private deployment fingerprint",
                ],
            )

    def test_authorship_does_not_exempt_other_rules(self) -> None:
        chat = "-100" + "7654321098"
        metadata = commit_metadata(
            author=f"Example {chat} <{PERSON_EMAIL}>",
            message=f"Example change\n\nReviewed-by: Example {HOME}{PRIVATE_TOKEN} <{PERSON_EMAIL}>\n",
        )
        self.assertEqual(
            sorted(rules(metadata)),
            ["non-placeholder Telegram chat ID", "owner-specific home path"],
        )

    def test_header_lookalike_in_message_is_not_identity(self) -> None:
        metadata = commit_metadata(message=f"Example\n\nauthor Someone <{PERSON_EMAIL}> 1 +0000\n")
        self.assertEqual(rules(metadata), ["non-example email address"])

    def test_trailer_shaped_prose_outside_the_final_block_is_scanned(self) -> None:
        for message in (
            f"Contact: Person <{PERSON_EMAIL}>\n",
            f"Change\n\nContact: Person <{PERSON_EMAIL}>\nThis is prose.\n",
            f"Change\n\nContact: Person <{PERSON_EMAIL}>\n\nMore prose.\n",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    rules(commit_metadata(message=message)), ["non-example email address"]
                )

    def test_mergetag_tagger_is_identity_but_its_message_is_not(self) -> None:
        identity = f"Example Person <{PERSON_EMAIL}>"
        metadata = (
            "tree "
            + "a" * 40
            + f"\nauthor {identity} 1 +0000\ncommitter {identity} 1 +0000\n"
            + "mergetag object "
            + "b" * 40
            + f"\n type commit\n tag v1\n tagger {identity} 1 +0000\n \n"
            + f" tagger {identity} 1 +0000\n\nMerge tag v1\n"
        ).encode()
        self.assertEqual(rules(metadata), ["non-example email address"])

    def test_non_utf8_metadata_is_reported(self) -> None:
        self.assertEqual(rules(b"\xff"), ["non-UTF-8 Git metadata"])


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

    def test_history_scan_accepts_authorship_and_rejects_message_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            root = Path(directory_name)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Example Person",
                "GIT_AUTHOR_EMAIL": PERSON_EMAIL,
                "GIT_COMMITTER_NAME": "Example Person",
                "GIT_COMMITTER_EMAIL": PERSON_EMAIL,
            }

            def git(*args: str, input_data: bytes | None = None) -> str:
                return (
                    subprocess.run(
                        ["git", *args],
                        cwd=root,
                        env=environment,
                        input=input_data,
                        capture_output=True,
                        check=True,
                    )
                    .stdout.decode()
                    .strip()
                )

            git("init", "-q")
            tree = git("mktree", input_data=b"")
            clean = git(
                "commit-tree",
                tree,
                input_data=f"Example\n\nCo-Authored-By: Example <{PERSON_EMAIL}>\n".encode(),
            )
            git("update-ref", "refs/heads/main", clean)
            self.assertEqual(scan_history(root), [])
            leaking = git(
                "commit-tree", tree, "-p", clean, input_data=f"Mail {PERSON_EMAIL}\n".encode()
            )
            git("update-ref", "refs/heads/main", leaking)
            self.assertEqual(
                [finding.rule for finding in scan_history(root)],
                ["non-example email address in Git commit metadata"],
            )


if __name__ == "__main__":
    unittest.main()
