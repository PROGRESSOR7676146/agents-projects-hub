"""The compatibility runtime consumes retained inputs but cannot produce new ones."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from hermes_codex_router.hub_config import HubConfigError
from tests import test_claude_image_controls as fixtures


class ClaudeRollbackConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudeImageConfigurationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_normal_configuration_and_explicit_disabled_input_remain_usable(self) -> None:
        with patch("subprocess.Popen", side_effect=AssertionError("no invocation")):
            self.fixture.load()
            self.fixture.load(claude_stream_input=False)

    def test_enabled_input_refuses_visibly_before_any_productive_invocation(self) -> None:
        with (
            patch("subprocess.Popen", side_effect=AssertionError("no invocation")),
            self.assertRaisesRegex(HubConfigError, "claude_stream_input.*rollback"),
        ):
            self.fixture.load(claude_stream_input=True)

    def test_nonboolean_input_is_not_silently_ignored(self) -> None:
        for value in (None, 1, 0, "false", {}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(HubConfigError, "claude_stream_input.*boolean"),
            ):
                self.fixture.load(claude_stream_input=value)


if __name__ == "__main__":
    unittest.main()
