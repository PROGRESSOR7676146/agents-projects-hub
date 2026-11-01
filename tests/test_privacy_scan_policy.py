from __future__ import annotations

import hashlib
import io
import multiprocessing
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from multiprocessing.process import BaseProcess
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.privacy_scan import (
    PrivacyFinding,
    _git_object_bytes_match_oid,
    _read_git_object,
    _read_git_objects,
    _scan_history_chunk,
    _scan_metadata_for_privacy,
    _start_history_pool,
    main,
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

    def blob_repository(self, root: Path, count: int) -> list[str]:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        ids = []
        for index in range(count):
            # Incompressible enough to fill the stdout pipe many times over.
            data = os.urandom(64 * 1024) + str(index).encode()
            ids.append(
                subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=root,
                    input=data,
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
        return ids

    def finish_within(self, action, seconds: float = 30.0) -> list[BaseException]:
        errors: list[BaseException] = []

        def run() -> None:
            try:
                action()
            except BaseException as error:  # noqa: BLE001 - reported to the test
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(seconds)
        self.assertFalse(worker.is_alive(), "batched Git read did not finish")
        return errors

    def test_batched_read_returns_every_object_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ids = self.blob_repository(root, 40)
            read = [object_id for object_id, kind, _ in _read_git_objects(root, ids)]
            self.assertEqual(read, ids)

    def test_early_close_and_missing_object_finish_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ids = self.blob_repository(root, 40)

            def close_early() -> None:
                stream = _read_git_objects(root, ids)
                next(stream)
                stream.close()

            self.assertEqual(self.finish_within(close_early), [])
            missing = ["0" * 40, *ids]
            errors = self.finish_within(lambda: list(_read_git_objects(root, missing)))
            self.assertEqual([type(error) for error in errors], [RuntimeError])

            def fail_in_body() -> None:
                for _ in _read_git_objects(root, ids):
                    raise ValueError("scan body failed")

            errors = self.finish_within(fail_in_body)
            self.assertEqual([type(error) for error in errors], [ValueError])

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


class HistoryScanBatchTests(unittest.TestCase):
    """One cat-file process and a worker pool must report exactly the serial findings."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Example Person",
            "GIT_AUTHOR_EMAIL": "person@example.com",
            "GIT_COMMITTER_NAME": "Example Person",
            "GIT_COMMITTER_EMAIL": "person@example.com",
        }
        self.git("init", "-q")

    def git(self, *args: str, input_data: bytes | None = None) -> str:
        return (
            subprocess.run(
                ["git", *args],
                cwd=self.root,
                env=self.environment,
                input=input_data,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )

    def commit(self, files: dict[str, bytes], message: str) -> str:
        for name, data in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def build_history(self) -> None:
        self.commit(
            {
                "docs/guide.md": b"Clean guide\n",
                "copy/guide.md": b"Clean guide\n",
                "notes.txt": f"Contact {PERSON_EMAIL}\n".encode(),
                "secrets/example.txt": b"placeholder\n",
                "image.bin": b"\0binary",
            },
            "Add fixtures",
        )
        self.commit({"notes.txt": b"Clean notes\n"}, f"Moved from {HOME}someone/work")
        self.git("tag", "-a", "v1", "-m", f"Release by {PERSON_EMAIL}")

    def test_parallel_chunks_match_serial_findings_in_order(self) -> None:
        self.build_history()
        serial = scan_history(self.root, jobs=1)
        self.assertEqual(
            sorted(finding.rule for finding in serial),
            sorted(
                [
                    "owner-specific home path in Git commit metadata",
                    "non-example email address in Git tag metadata",
                    "binary tracked file requires explicit review in historical blob "
                    + self.git("rev-parse", "HEAD:image.bin")[:12],
                    "local runtime or secret-bearing file is forbidden in historical blob "
                    + self.git("rev-parse", "HEAD:secrets/example.txt")[:12],
                    "non-example email address in historical blob "
                    + self.git("rev-parse", "HEAD~1:notes.txt")[:12],
                ]
            ),
        )
        # Tiny chunks force several worker tasks; order must not depend on them.
        with patch("hermes_codex_router.privacy_scan._HISTORY_CHUNK_BYTES", 1):
            self.assertEqual(scan_history(self.root, jobs=3), serial)
        self.assertEqual(scan_history(self.root), serial)

    def test_batch_read_ignores_replace_mapping(self) -> None:
        leaking = self.commit({"notes.txt": f"Contact {PERSON_EMAIL}\n".encode()}, "Leak")
        blob = self.git("rev-parse", f"{leaking}:notes.txt")
        clean = self.git("hash-object", "-w", "--stdin", input_data=b"Clean\n")
        self.git("replace", blob, clean)
        self.assertEqual(
            [finding.rule for finding in scan_history(self.root, jobs=2)],
            [f"non-example email address in historical blob {blob[:12]}"],
        )

    def test_mismatched_object_bytes_are_reported_not_scanned(self) -> None:
        object_id = hashlib.sha1(b"blob 6\0Clean\n", usedforsecurity=False).hexdigest()
        findings = _scan_history_chunk(
            [(object_id, "blob", "notes.txt", f"Contact {PERSON_EMAIL}\n".encode())]
        )
        self.assertEqual(
            findings,
            [PrivacyFinding(Path("notes.txt"), 1, "Git object bytes do not match object ID")],
        )

    def test_unavailable_worker_pool_falls_back_to_the_same_serial_scan(self) -> None:
        self.build_history()
        serial = scan_history(self.root, jobs=1)
        for error in (
            OSError("example: no process pool in this sandbox"),
            ImportError("example: this platform lacks a functioning sem_open"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("hermes_codex_router.privacy_scan.ProcessPoolExecutor", side_effect=error),
            ):
                self.assertEqual(scan_history(self.root, jobs=4), serial)

    def test_workers_run_at_lowest_cpu_priority(self) -> None:
        caller = os.nice(0)
        executor = _start_history_pool(2)
        assert executor is not None
        with executor:
            self.assertEqual(executor.submit(os.nice, 0).result(), 19)
        # Only the workers yield; the caller keeps its own priority.
        self.assertEqual(os.nice(0), caller)

    def test_cli_passes_bounded_jobs_to_the_history_scan(self) -> None:
        with (
            patch("hermes_codex_router.privacy_scan.scan_repository", return_value=[]),
            patch("hermes_codex_router.privacy_scan.scan_history", return_value=[]) as history,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main([str(self.root), "--history", "--jobs", "2"]), 0)
            self.assertEqual(main([str(self.root), "--history"]), 0)
            self.assertEqual([call.kwargs["jobs"] for call in history.call_args_list], [2, None])
            for jobs in ("0", "33", "many"):
                with (
                    self.subTest(jobs=jobs),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    main([str(self.root), "--history", "--jobs", jobs])
                self.assertEqual(error.exception.code, 2)

    def test_partial_worker_start_failure_stops_started_workers(self) -> None:
        self.build_history()
        serial = scan_history(self.root, jobs=1)
        fork = multiprocessing.get_context("fork")
        started: list[BaseProcess] = []

        class SecondStartFails(type(fork).Process):  # type: ignore[misc]
            def start(self) -> None:
                if started:
                    raise OSError("example: process limit reached")
                started.append(self)
                super().start()

        class FailingContext(type(fork)):  # type: ignore[misc]
            Process = SecondStartFails

        with patch(
            "hermes_codex_router.privacy_scan.multiprocessing.get_context",
            return_value=FailingContext(),
        ):
            self.assertEqual(scan_history(self.root, jobs=3), serial)
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].is_alive())

    def test_submission_is_bounded_and_ordered(self) -> None:
        self.build_history()
        serial = scan_history(self.root, jobs=1)
        submitted = threading.Semaphore(0)
        outstanding: list[int] = []
        real_submit = ProcessPoolExecutor.submit

        def counting_submit(executor, fn, *args):  # type: ignore[no-untyped-def]
            future = real_submit(executor, fn, *args)
            if fn is not int:
                outstanding.append(1)
                future.add_done_callback(lambda _: outstanding.pop())
                self.assertLessEqual(len(outstanding), 2 * 2)
                submitted.release()
            return future

        with (
            patch("hermes_codex_router.privacy_scan._HISTORY_CHUNK_BYTES", 1),
            patch.object(ProcessPoolExecutor, "submit", counting_submit),
        ):
            self.assertEqual(scan_history(self.root, jobs=2), serial)
        self.assertTrue(submitted.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
