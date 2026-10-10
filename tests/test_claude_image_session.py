"""Exact native images/resume is opt-in; incomplete evidence fails closed."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.claude_image_session_fixture import (
    build_image_fixture_argv,
    run_image_session_fixture,
    validate_image_evidence,
)
from tests.claude_native_transport_fixture import (
    NativeTransportFixtureError,
    build_native_fixture_argv,
)


class ImageSessionEvidenceTests(unittest.TestCase):
    def report(self) -> dict:
        return {
            "native_version": "2.1.285 (Claude Code)",
            "host_files_hidden": True,
            "host_loopback_blocked": True,
            "ports_distinct": True,
            "completed": [True, True],
            "missing_no_messages": True,
            "missing_refusal_validated": True,
            "missing_store_unchanged": True,
            "missing_exit_code": 0,
            "missing_requests": 0,
            "missing_heads": 0,
            "connections": 2,
            "timeouts": 0,
            "requests": 2,
            "heads": 0,
            "posts": 2,
            "messages_served": 2,
            "violations": 0,
            "validated_requests": 2,
        }

    def test_complete_bounded_evidence_accepts(self) -> None:
        self.assertEqual(validate_image_evidence(self.report()), self.report())
        passive = {
            **self.report(),
            "requests": 5,
            "heads": 3,
            "connections": 5,
            "missing_requests": 1,
            "missing_heads": 1,
            "missing_exit_code": 1,
        }
        self.assertEqual(validate_image_evidence(passive), passive)

    def test_unknown_terminal_retry_or_raw_diagnostic_refuses(self) -> None:
        for change in (
            {"completed": [True, False]},
            {"completed": [1, 1]},
            {"missing_no_messages": False},
            {"missing_refusal_validated": False},
            {"missing_store_unchanged": False},
            {"missing_exit_code": -9},
            {"missing_exit_code": True},
            {"host_files_hidden": False},
            {"host_loopback_blocked": False},
            {"ports_distinct": False},
            {"posts": 3},
            {"validated_requests": 1},
            {"messages_served": True},
            {"requests": 3},
            {"heads": 4},
            {"missing_heads": 2, "missing_requests": 2},
            {"missing_heads": 0, "missing_requests": 1},
            {"connections": 0},
            {"timeouts": 1},
            {"violations": 1},
            {"native_version": "2.1.284 (Claude Code)"},
            {"raw": "example-native-text"},
        ):
            with self.subTest(change=change), self.assertRaises(NativeTransportFixtureError):
                validate_image_evidence({**self.report(), **change})

    def test_only_input_shape_and_persistence_differ_from_existing_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-image-argv-") as directory:
            cwd = Path(directory)
            old = list(build_native_fixture_argv(cwd))
            image = list(build_image_fixture_argv(cwd))
        old.remove("--no-session-persistence")
        old = old[: old.index("--")] + ["--input-format", "stream-json"]
        self.assertEqual(image, old)
        self.assertNotIn("--resume", image)
        self.assertNotIn("--fork-session", image)
        self.assertEqual(image[image.index("--tools") + 1], "")


class NativeClaudeImageSessionTests(unittest.TestCase):
    def test_pinned_native_images_exact_saved_resume_and_missing_source(self) -> None:
        executable = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE")
        digest = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_SHA256")
        version = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_VERSION")
        if not all((executable, digest, version)):
            reason = "explicit native image witness requires executable, SHA256 and version"
            if os.environ.get("HUB_REQUIRE_NATIVE_CLAUDE_IMAGE_TESTS") == "1":
                self.fail(reason)
            self.skipTest(reason)
        assert executable and digest and version
        report = run_image_session_fixture(
            Path(executable), expected_sha256=digest, expected_version=version
        )
        print(json.dumps(report), flush=True)
