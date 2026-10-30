from __future__ import annotations

import unittest
from unittest.mock import patch

from hermes_codex_router.hub_config import HubConfigError
from tests import test_claude_image_controls as fixtures


class ClaudeCorrectionConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudeImageConfigurationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_default_and_explicit_boolean_gate(self) -> None:
        self.assertFalse(self.fixture.load().claude_stream_input)
        self.assertFalse(self.fixture.load(claude_stream_input=False).claude_stream_input)
        self.assertTrue(self.fixture.load(claude_stream_input=True).claude_stream_input)

    def test_invalid_values_image_combination_and_missing_worker_fail_without_invocation(
        self,
    ) -> None:
        invalid: list[dict[str, object]] = [
            dict(claude_stream_input=value) for value in (None, 1, "true", {})
        ]
        invalid.extend(
            [
                dict(claude_stream_input=True, claude_image_input=True),
                dict(claude_stream_input=True, external_worker_agent_ids=["codex"]),
            ]
        )
        with patch("subprocess.Popen", side_effect=AssertionError("no invocation")):
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(HubConfigError):
                    self.fixture.load(**value)


if __name__ == "__main__":
    unittest.main()
