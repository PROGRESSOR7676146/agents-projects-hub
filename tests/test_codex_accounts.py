from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_codex_router.codex_accounts import (
    CodexAccountStatus,
    CodexPoolStatus,
    decode_codex_pool_snapshot,
    encode_codex_pool_snapshot,
    format_codex_pool_status,
    read_codex_pool_status,
)


class CodexAccountStatusTests(unittest.TestCase):
    def test_safe_snapshot_round_trip_contains_only_masked_status(self) -> None:
        original = CodexPoolStatus(
            True,
            True,
            (
                CodexAccountStatus(
                    1,
                    True,
                    "unavailable",
                    "high",
                    80,
                    60,
                    1_800_000_000,
                    None,
                    None,
                    False,
                    "abc…",
                    auth_invalidated=True,
                ),
            ),
            1,
            2,
        )

        encoded = encode_codex_pool_snapshot(original)
        restored = decode_codex_pool_snapshot(encoded)

        self.assertEqual(restored, original)
        self.assertLessEqual(len(encoded), 1000)

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
                            "reasons": ["token-invalid — re-login needed"],
                        }
                    ],
                },
                "runtime": {"runtimeMetrics": {"accountRotations": 4}},
            }

            def runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess([], 0, json.dumps(report), "")

            status = read_codex_pool_status(root, runner=runner, identity_hints={1: "acc"})
            rendered = format_codex_pool_status(status, timezone_name="Europe/Moscow")

        self.assertTrue(status.available)
        self.assertTrue(status.rotation_enabled)
        self.assertEqual(status.recommended_account, 1)
        self.assertEqual(status.account_rotations, 4)
        self.assertEqual(status.accounts[0].five_hour_remaining, 75)
        self.assertEqual(status.accounts[0].weekly_remaining, 60)
        self.assertEqual(status.accounts[0].identity_hint, "acc…")
        self.assertTrue(status.accounts[0].auth_invalidated)
        self.assertNotIn("secret@example.com", rendered)
        self.assertNotIn("ABC123", rendered)
        self.assertIn("acc…", rendered)

    def test_cli_failure_is_non_fatal_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired("contains-secret", 10)

            status = read_codex_pool_status(root, runner=runner)

        self.assertFalse(status.available)
        self.assertEqual(status.error, "TimeoutExpired")

    def test_live_report_overrides_stale_cache_and_uses_selected_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(
                json.dumps({"pluginConfig": {"codexRuntimeRotationProxy": True}})
            )
            (root / "quota-cache.json").write_text(json.dumps({"byAccountId": {}}))
            report = {
                "generatedAt": "2026-09-03T18:03:24+00:00",
                "forecast": {
                    "recommendation": {"recommendedIndex": 1},
                    "accounts": [
                        {
                            "index": 0,
                            "label": "Account 1 [id:ABC123]",
                            "isCurrent": True,
                            "selected": False,
                            "availability": "delayed",
                            "riskLevel": "medium",
                            "liveQuota": {
                                "summary": "5h 0% left (resets 00:33 on Sep 04), "
                                "7d 84% left (resets 19:33 on Sep 10), plan:plus"
                            },
                        },
                        {
                            "index": 1,
                            "label": "Account 2 [id:XYZ789]",
                            "isCurrent": False,
                            "selected": True,
                            "availability": "ready",
                            "riskLevel": "low",
                            "liveQuota": {
                                "summary": "5h 86% left (resets 21:09), "
                                "7d 17% left (resets 08:34 on Sep 07), plan:plus"
                            },
                        },
                    ],
                },
                "runtime": {"runtimeMetrics": {"accountRotations": 1}},
            }
            calls: list[tuple[str, ...]] = []

            def runner(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(argv)
                return subprocess.CompletedProcess([], 0, json.dumps(report), "")

            status = read_codex_pool_status(
                root,
                runner=runner,
                live=True,
                timezone_name="Europe/Moscow",
                identity_hints={1: "one", 2: "two"},
            )

        self.assertIn("--live", calls[0])
        self.assertFalse(status.accounts[0].active)
        self.assertTrue(status.accounts[1].active)
        self.assertEqual(status.accounts[0].five_hour_remaining, 0)
        self.assertEqual(status.accounts[1].weekly_remaining, 17)
        self.assertFalse(status.accounts[1].quota_stale)
        self.assertEqual(
            status.accounts[1].five_hour_resets_at,
            int(datetime(2026, 9, 3, 21, 9, tzinfo=ZoneInfo("Europe/Moscow")).timestamp()),
        )


if __name__ == "__main__":
    unittest.main()
