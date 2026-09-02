from __future__ import annotations

import unittest

from hermes_codex_router.codex_accounts import CodexAccountStatus, CodexPoolStatus
from hermes_codex_router.codex_appserver import LimitWindow, RateLimits
from hermes_codex_router.provider_limits import ProviderLimit
from hermes_codex_router.status_view import (
    cached_codex_rate_limits,
    format_accounts,
    format_session_status,
)


class StatusViewTests(unittest.TestCase):
    def test_cached_codex_account_projects_into_compact_rate_limits(self) -> None:
        account = CodexAccountStatus(
            1,
            True,
            "ready",
            "low",
            14,
            62,
            1_800_000_000,
            1_800_100_000,
            1_799_900_000,
            True,
            "abc…",
        )

        limits = cached_codex_rate_limits(account)

        assert limits.primary is not None and limits.secondary is not None
        self.assertEqual(limits.primary.remaining_percent, 14)
        self.assertEqual(limits.primary.resets_at, 1_800_000_000)
        self.assertEqual(limits.secondary.remaining_percent, 62)
        self.assertEqual(limits.secondary.resets_at, 1_800_100_000)

    def test_stale_status_limits_are_yellow_and_labelled_cached(self) -> None:
        text = format_session_status(
            agent="Codex",
            model="gpt-5.6-sol",
            effort="high",
            writer="telegram",
            context_remaining=None,
            account_hint="abc…",
            limits=RateLimits(
                LimitWindow(90, 1_800_000_000, None),
                LimitWindow(80, 1_800_100_000, None),
            ),
            timezone_name="UTC",
            limits_stale=True,
        )

        self.assertIn("🟡 5h 90%", text)
        self.assertIn("🟡 Week 80%", text)
        self.assertEqual(text.count("· cached"), 2)

    def test_compact_status_omits_technical_provider_noise(self) -> None:
        text = format_session_status(
            agent="Codex",
            model="gpt-5.6-sol",
            effort="high",
            writer="telegram",
            context_remaining=73.25,
            account_hint="acc…",
            limits=RateLimits(
                LimitWindow(83, 1_800_000_000, 300),
                LimitWindow(64, 1_800_600_000, 10080),
            ),
            timezone_name="Europe/Moscow",
        )
        self.assertTrue(text.startswith("Codex · GPT-5.6 Sol · High"))
        self.assertIn("Context 73.2% · Account acc…", text)
        self.assertIn("5h 83%", text)
        self.assertIn("🟢 5h", text)
        self.assertNotIn("provider", text.lower())

    def test_status_surfaces_known_network_unavailability_compactly(self) -> None:
        text = format_session_status(
            agent="Antigravity",
            model="gemini-3.7-flash",
            effort="high",
            writer="telegram",
            context_remaining=100,
            account_hint="abc…",
            limits=RateLimits(LimitWindow(100, 2_000_000_000, None), None),
            timezone_name="UTC",
            provider_state="unavailable",
            provider_error_code="unsupported_network_location",
        )

        self.assertIn("🔴 Current network location unsupported", text)
        self.assertIn("🟢 5h 100%", text)

    def test_accounts_lists_codex_and_opencode_go_capabilities(self) -> None:
        pool = CodexPoolStatus(
            True,
            True,
            (CodexAccountStatus(1, True, "ready", "low", 83, 64, None, None, None, False, "acc…"),),
            1,
            0,
        )
        text = format_accounts(pool, include_opencode_go=True)
        self.assertIn("Codex", text)
        self.assertIn("✓ acc…", text)
        self.assertIn("OpenCode Go", text)
        self.assertIn("🟢 plan", text)
        self.assertIn("plan: 5h $12", text)

    def test_accounts_shows_latest_provider_supplied_opencode_reset(self) -> None:
        pool = CodexPoolStatus(False, False, (), None, 0, "not configured")
        text = format_accounts(
            pool,
            include_opencode_go=True,
            opencode_limit=ProviderLimit("opencode-go", "monthly", 0, 1788040800),
            timezone_name="UTC",
        )
        self.assertIn("Month 0%", text)
        self.assertIn("↻", text)
        self.assertIn("5h $12 · week $30 · month $60", text)

    def test_accounts_lists_antigravity_accounts_and_known_runtime_limit(self) -> None:
        pool = CodexPoolStatus(False, False, (), None, 0, "not configured")
        text = format_accounts(
            pool,
            include_opencode_go=False,
            provider_account_hints={"antigravity": ("abc", "xyz")},
            provider_limits={
                "antigravity": ProviderLimit("antigravity", "individual", 0, 2_000_000_000)
            },
            timezone_name="UTC",
        )
        self.assertIn("Antigravity", text)
        self.assertIn("🟡 abc… · limits unknown", text)
        self.assertIn("🟡 xyz… · limits unknown", text)
        self.assertIn("🔴 current account unknown · quota 0%", text)

    def test_accounts_marks_the_matching_telemetry_account_and_quota(self) -> None:
        pool = CodexPoolStatus(False, False, (), None, 0, "not configured")
        text = format_accounts(
            pool,
            include_opencode_go=False,
            provider_account_hints={"antigravity": ("abc", "xyz")},
            provider_limits={
                "antigravity": ProviderLimit("antigravity", "model", 73, 2_000_000_000)
            },
            provider_current_accounts={"antigravity": "abc…"},
            timezone_name="UTC",
        )
        self.assertIn("🟢 ✓ abc… · quota 73%", text)
        self.assertIn("🟡 xyz… · limits unknown", text)
        self.assertNotIn("current account unknown", text)

    def test_accounts_does_not_present_quota_as_provider_availability(self) -> None:
        pool = CodexPoolStatus(False, False, (), None, 0, "not configured")
        text = format_accounts(
            pool,
            include_opencode_go=False,
            provider_account_hints={"antigravity": ("abc", "xyz")},
            provider_limits={
                "antigravity": ProviderLimit("antigravity", "model", 100, 2_000_000_000)
            },
            provider_current_accounts={"antigravity": "abc…"},
            provider_states={"antigravity": "unavailable"},
            provider_error_codes={"antigravity": "unsupported_network_location"},
            timezone_name="UTC",
        )

        self.assertIn("🔴 Current network location unsupported", text)
        self.assertIn("🔴 ✓ abc… · quota 100%", text)
