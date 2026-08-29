from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.codex_accounts import (
    format_codex_pool_status,
    read_codex_pool_status,
)


class CodexAccountStatusTests(unittest.TestCase):
    def test_redacts_identity_and_reports_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(
                json.dumps({"pluginConfig": {"codexRuntimeRotationProxy": True}})
            )
            (root / "quota-cache.json").write_text(
                json.dumps(
                    {
                        "byAccountId": {
                            "org-secret-ABC123": {
                                "updatedAt": 1_700_000_000_000,
                                "primary": {"usedPercent": 25, "resetAtMs": 1_700_001_000_000},
                                "secondary": {"usedPercent": 40, "resetAtMs": 1_700_002_000_000},
                            }
                        }
                    }
                )
            )
            report = {
                "forecast": {
                    "recommendation": {"recommendedIndex": 0},
                    "accounts": [
                        {
                            "index": 0,
                            "label": "Account 1 (secret@example.com [id:ABC123])",
                            "isCurrent": True,
                            "availability": "ready",
                            "riskLevel": "low",
                        }
                    ],
                },
                "runtime": {"runtimeMetrics": {"accountRotations": 4}},
            }

            def runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess([], 0, json.dumps(report), "")

            status = read_codex_pool_status(root, runner=runner)
            rendered = format_codex_pool_status(status, timezone_name="Europe/Moscow")

        self.assertTrue(status.available)
        self.assertTrue(status.rotation_enabled)
        self.assertEqual(status.recommended_account, 1)
        self.assertEqual(status.account_rotations, 4)
        self.assertEqual(status.accounts[0].five_hour_remaining, 75)
        self.assertEqual(status.accounts[0].weekly_remaining, 60)
        self.assertEqual(status.accounts[0].identity_hint, "se***@***.com")
        self.assertNotIn("secret@example.com", rendered)
        self.assertNotIn("ABC123", rendered)
        self.assertIn("se***@***.com", rendered)

    def test_cli_failure_is_non_fatal_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired("contains-secret", 10)

            status = read_codex_pool_status(root, runner=runner)

        self.assertFalse(status.available)
        self.assertEqual(status.error, "TimeoutExpired")


if __name__ == "__main__":
    unittest.main()
