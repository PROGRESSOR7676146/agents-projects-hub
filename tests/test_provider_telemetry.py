from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.hub_config import ProviderTelemetrySettings
from hermes_codex_router.provider_telemetry import (
    load_antigravity_telemetry,
    probe_antigravity_telemetry,
)


class ProviderTelemetryTests(unittest.TestCase):
    def test_reads_private_structured_antigravity_cache_without_ansi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota = root / "quota.json"
            status = root / "status.json"
            quota.write_text(
                json.dumps(
                    {
                        "timestamp": 1000,
                        "scope": {"email": "abc-user@example.com"},
                        "models": {
                            "gemini37flashhigh": {
                                "remaining_percentage": 71.4,
                                "reset_time": "1970-01-01T00:30:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            status.write_text(
                json.dumps(
                    {
                        "timestamp": 1000,
                        "model": "Gemini 3.7 Flash (High)",
                        "context_remaining_percentage": 63.25,
                    }
                ),
                encoding="utf-8",
            )
            quota.chmod(0o600)
            status.chmod(0o600)
            telemetry = load_antigravity_telemetry(
                ProviderTelemetrySettings(quota, status),
                selected_model="gemini-3.7-flash",
                selected_effort="high",
                now=1100,
            )
        self.assertEqual(telemetry.account_hint, "abc…")
        self.assertEqual(telemetry.context_remaining, 63.25)
        self.assertEqual(telemetry.quota_remaining, 71)
        self.assertEqual(telemetry.quota_resets_at, 1800)
        self.assertEqual(telemetry.effort, "high")

    def test_rejects_stale_or_world_readable_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota = root / "quota.json"
            status = root / "status.json"
            quota.write_text('{"timestamp":1}', encoding="utf-8")
            status.write_text('{"timestamp":1}', encoding="utf-8")
            quota.chmod(0o644)
            status.chmod(0o600)
            telemetry = load_antigravity_telemetry(
                ProviderTelemetrySettings(quota, status),
                selected_model="provider-selected",
                selected_effort="high",
                now=10000,
            )
        self.assertIsNone(telemetry.quota_remaining)
        self.assertTrue(telemetry.stale)

    def test_probe_distinguishes_fresh_private_sources_from_stale_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota = root / "quota.json"
            status = root / "status.json"
            quota.write_text('{"timestamp":1000}', encoding="utf-8")
            status.write_text('{"timestamp":1000}', encoding="utf-8")
            quota.chmod(0o600)
            status.chmod(0o600)
            fresh = probe_antigravity_telemetry(ProviderTelemetrySettings(quota, status), now=1100)
            stale = probe_antigravity_telemetry(ProviderTelemetrySettings(quota, status), now=10000)

        self.assertTrue(fresh.ok)
        self.assertEqual(fresh.detail, "quota=fresh, status=fresh")
        self.assertFalse(stale.ok)
        self.assertEqual(stale.detail, "quota=stale, status=stale")

    def test_probe_rejects_unsafe_or_invalid_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota = root / "quota.json"
            status = root / "status.json"
            quota.write_text("{}", encoding="utf-8")
            status.write_text("not json", encoding="utf-8")
            quota.chmod(0o644)
            status.chmod(0o600)
            health = probe_antigravity_telemetry(ProviderTelemetrySettings(quota, status), now=1000)

        self.assertFalse(health.ok)
        self.assertEqual(health.detail, "quota=unsafe-permissions, status=invalid-json")


if __name__ == "__main__":
    unittest.main()
