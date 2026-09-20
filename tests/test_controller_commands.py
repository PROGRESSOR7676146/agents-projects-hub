from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from hermes_codex_router.codex_accounts import CodexAccountStatus, CodexPoolStatus
from hermes_codex_router.codex_appserver import LimitWindow, RateLimits
from hermes_codex_router.controller_commands import (
    ControllerCommandOrchestrator,
    HtmlCommandDecision,
    TextCommandDecision,
)
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.model_selection import ModelSelectionError
from hermes_codex_router.provider_catalog_cache import CachedProviderModel, CatalogSnapshot
from hermes_codex_router.provider_limits import ProviderLimit
from hermes_codex_router.state import HubState, StateError


class ControllerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(101,),
            registry_path=self.base / "projects.json",
            state_path=self.base / "state.db",
            codex_socket_path=self.base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Example Linux"),
            projects=(ProjectBinding("example-project", -1001234567890),),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-example",
                    "high",
                ),
                AgentDefinition(
                    "opencode",
                    "OpenCode",
                    "example_opencode_bot",
                    "opencode",
                    None,
                    True,
                    False,
                    "opencode-example",
                    "default",
                ),
            ),
        )
        self.state = HubState.open(self.config.state_path)
        self.addCleanup(self.state.close)
        self.addCleanup(self.tempdir.cleanup)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            title="Example topic",
        )
        self.orchestrator = ControllerCommandOrchestrator(self.config, self.state)

    @staticmethod
    def _pool() -> CodexPoolStatus:
        return CodexPoolStatus(
            True,
            True,
            (
                CodexAccountStatus(
                    1,
                    True,
                    "ready",
                    "low",
                    83,
                    64,
                    1_800_000_000,
                    1_800_100_000,
                    1_800_000_000,
                    False,
                    "exa…",
                ),
            ),
            1,
            0,
        )

    @staticmethod
    def _catalog(agent_id: str = "codex") -> CatalogSnapshot:
        return CatalogSnapshot(
            agent_id,
            (
                CachedProviderModel("gpt-example", "GPT Example", ("low", "high"), "key-one"),
                CachedProviderModel("gpt-next", "GPT Next", ("medium", "high"), "key-two"),
            ),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
            "fictional-source",
            None,
        )

    @staticmethod
    def _buttons(decision: HtmlCommandDecision) -> list[dict[str, str]]:
        rows = cast(list[list[dict[str, str]]], decision.reply_markup["inline_keyboard"])
        return [button for row in rows for button in row]

    def test_empty_status_is_deterministic_and_has_no_sender_identity(self) -> None:
        decision = self.orchestrator.status(self.topic, self._pool(), RateLimits(None, None))
        self.assertIsInstance(decision, TextCommandDecision)
        self.assertEqual(decision.text, "No active agent session has been created yet.")
        self.assertIsNone(decision.response_agent_id)

    def test_status_uses_supplied_cached_identity_without_provider_access(self) -> None:
        session = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-example", "high")
        self.state.set_context_remaining(session.session_id, 73.25)
        decision = self.orchestrator.status(
            self.topic,
            self._pool(),
            RateLimits(LimitWindow(83, 1_800_000_000, 300), LimitWindow(64, 1_800_100_000, 10080)),
        )
        self.assertEqual(decision.response_agent_id, "codex")
        self.assertIn("Codex · Gpt Example · High", decision.text)
        self.assertIn("Context 73.2% · Account exa…", decision.text)
        self.assertIn("5h 83%", decision.text)

    def test_accounts_is_cache_only_and_masks_the_account(self) -> None:
        decision = self.orchestrator.accounts(self._pool())
        self.assertIsInstance(decision, TextCommandDecision)
        self.assertIn("Codex", decision.text)
        self.assertIn("exa…", decision.text)
        self.assertNotIn("1_800_000_000", decision.text)

    def test_accounts_uses_one_injected_time_for_cached_limit_expiry(self) -> None:
        self.state.record_runtime_event(
            "opencode",
            "warning",
            "provider_limit",
            ProviderLimit("opencode-go", "5-hour", 0, 2_000).to_json(),
        )
        current = self.orchestrator.accounts(self._pool(), now=1_000)
        expired = self.orchestrator.accounts(self._pool(), now=3_000)
        self.assertIn("5h 0%", current.text)
        self.assertNotIn("5h 0%", expired.text)

    def test_provider_model_and_effort_callbacks_are_exact(self) -> None:
        active = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-example", "high")
        providers = self.orchestrator.provider_menu(self.topic)
        self.assertEqual(
            [button["callback_data"] for button in self._buttons(providers)],
            ["provider:codex", "provider:opencode"],
        )
        self.assertTrue(self._buttons(providers)[0]["text"].startswith("✓ "))

        catalog = self._catalog()
        models = self.orchestrator.model_menu(self.topic, "codex", catalog)
        self.assertEqual(
            [button["callback_data"] for button in self._buttons(models)],
            ["choose:codex:key-one", "choose:codex:key-two", "modelrefresh:codex:0"],
        )
        efforts = self.orchestrator.effort_menu(self.topic, "codex", "key-two", catalog)
        self.assertEqual(
            [button["callback_data"] for button in self._buttons(efforts)],
            ["use:codex:key-two:medium", "use:codex:key-two:high"],
        )
        current = self.state.active_session(self.topic.topic_id)
        assert current is not None
        self.assertEqual(current.session_id, active.session_id)

    def test_apply_uses_the_displayed_catalog_snapshot_exactly(self) -> None:
        old = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-example", "high")
        result = self.orchestrator.apply_model_selection(
            self.topic,
            "codex",
            "key-two",
            "medium",
            self._catalog(),
            expected_session_id=old.session_id,
        )
        self.assertIsInstance(result, TextCommandDecision)
        current = self.state.active_session(self.topic.topic_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual((current.model, current.effort), ("gpt-next", "medium"))
        self.assertEqual(self.state.get_session(old.session_id).status, "archived")

    def test_unknown_key_and_stale_session_are_rejected_before_mutation(self) -> None:
        old = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-example", "high")
        with self.assertRaises(ModelSelectionError):
            self.orchestrator.apply_model_selection(
                self.topic,
                "codex",
                "unknown-key",
                "high",
                self._catalog(),
                expected_session_id=old.session_id,
            )
        with self.assertRaises(StateError):
            self.orchestrator.apply_model_selection(
                self.topic,
                "codex",
                "key-two",
                "medium",
                self._catalog(),
                expected_session_id="stale-session",
            )
        current = self.state.active_session(self.topic.topic_id)
        assert current is not None
        self.assertEqual(current.session_id, old.session_id)

    def test_local_writer_rejects_model_change(self) -> None:
        active = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-example", "high")
        self.state.set_writer_mode(active.session_id, "local")
        with self.assertRaises(StateError):
            self.orchestrator.apply_model_selection(
                self.topic,
                "codex",
                "key-two",
                "medium",
                self._catalog(),
                expected_session_id=active.session_id,
            )


if __name__ == "__main__":
    unittest.main()
