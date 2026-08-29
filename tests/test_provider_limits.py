from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_codex_router.external_runtime import ExternalCliAdapter, ProviderLimitError
from hermes_codex_router.provider_limits import parse_opencode_limit


class ProviderLimitTests(unittest.TestCase):
    def test_parses_opencode_go_provider_reset(self) -> None:
        now = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
        value = parse_opencode_limit(
            "HTTP 429: 5-hour usage limit reached. Resets in 2hr 44min.", now=now
        )
        assert value is not None
        self.assertEqual(value.window, "5-hour")
        self.assertEqual(value.remaining_percent, 0)
        self.assertEqual(value.resets_at, int(now.timestamp()) + 9840)

    def test_adapter_classifies_provider_429_without_exposing_raw_error(self) -> None:
        def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 1, "", "HTTP 429: Monthly usage limit reached. Resets in 25min."
            )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProviderLimitError) as raised:
                ExternalCliAdapter("opencode", run=run).run_turn(
                    cwd=Path(directory), prompt="hello"
                )
        self.assertEqual(raised.exception.limit.window, "monthly")


if __name__ == "__main__":
    unittest.main()
