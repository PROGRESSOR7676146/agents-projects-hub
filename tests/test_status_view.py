from __future__ import annotations

import unittest

from hermes_codex_router.codex_accounts import CodexAccountStatus, CodexPoolStatus
from hermes_codex_router.codex_appserver import LimitWindow, RateLimits
from hermes_codex_router.provider_limits import ProviderLimit
from hermes_codex_router.status_view import format_accounts, format_session_status


class StatusViewTests(unittest.TestCase):
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
