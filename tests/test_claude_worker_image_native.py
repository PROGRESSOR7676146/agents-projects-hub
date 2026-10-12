"""Production input/process native witness remains explicitly offline and opt-in."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests import test_claude_image_session as base_evidence
from tests.claude_image_session_fixture import (
    run_image_session_fixture,
    validate_worker_image_evidence,
)
from tests.claude_native_transport_fixture import NativeTransportFixtureError


class WorkerImageEvidenceTests(unittest.TestCase):
    def report(self) -> dict:
        return {
            "base": base_evidence.ImageSessionEvidenceTests().report(),
            "production_input": True,
            "maximum_input": True,
            "corrupt_outcomes": ["guarded_downgrade"] * 3,
        }

    def test_verified_transfer_or_explicit_refusal_is_required_for_both_formats(self) -> None:
        report = self.report()
        validated = validate_worker_image_evidence(report)
        self.assertEqual(validated["corrupt_outcomes"], report["corrupt_outcomes"])
        self.assertTrue(validated["production_input"])
        for change in (
            {"production_input": 1},
            {"maximum_input": False},
            {"corrupt_outcomes": ["typed_refusal"]},
            {"corrupt_outcomes": ["typed_refusal", "text_only"]},
            {"corrupt_outcomes": ["typed_refusal", {}]},
            {"corrupt_outcomes": ["typed_refusal", "parser_error"]},
            {"raw_output": "example-native-text"},
            {"base": {**report["base"], "completed": [True, False]}},
        ):
            with self.subTest(change=change), self.assertRaises(NativeTransportFixtureError):
                validate_worker_image_evidence({**report, **change})


class NativeClaudeWorkerImageTests(unittest.TestCase):
    def test_pinned_production_encoder_process_resume_and_corrupt_images(self) -> None:
        executable = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE")
        digest = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_SHA256")
        version = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_VERSION")
        if not all((executable, digest, version)):
            reason = "explicit production-input witness requires executable, SHA256 and version"
            if os.environ.get("HUB_REQUIRE_NATIVE_CLAUDE_WORKER_IMAGE_TESTS") == "1":
                self.fail(reason)
            self.skipTest(reason)
        assert executable and digest and version
        report = run_image_session_fixture(
            Path(executable),
            expected_sha256=digest,
            expected_version=version,
            worker_input=True,
        )
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    unittest.main()
