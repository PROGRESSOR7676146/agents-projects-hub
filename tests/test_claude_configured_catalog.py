from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import replace
from unittest.mock import patch

from hermes_codex_router.catalog_refresh import refresh_provider_catalogs
from hermes_codex_router.hub_config import HubConfigError, load_hub_config
from hermes_codex_router.provider_catalog import ProviderCatalogError, ProviderModel
from hermes_codex_router.provider_catalog_cache import ProviderCatalogCache
from hermes_codex_router.telegram import parse_topic_message
from tests import test_claude_model_continuity as continuity_fixtures
from tests import test_hub_config as config_fixtures

CATALOG = (
    ProviderModel("example-default", "Example default", ("high",)),
    ProviderModel("example-second", "Example second", ("low", "medium", "xhigh", "max")),
)


def entries(models=CATALOG):
    return [
        {"model_id": model.model_id, "label": model.label, "efforts": list(model.efforts)}
        for model in models
    ]


class ClaudeCatalogConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = config_fixtures.HubConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def load(self, **changes):
        path = self.fixture.write_config()
        document = json.loads(path.read_text())
        claude = dict(document["agents"][0])
        claude_token = self.fixture.base / "example-claude-token"
        claude_token.write_text("654321:example-claude-token")
        claude_token.chmod(0o600)
        claude.update(
            agent_id="claude",
            display_name="Claude",
            telegram_username="example_claude_bot",
            runtime="claude",
            default_model="example-default",
            default_effort="high",
            terminal_enabled=False,
            token_file=str(claude_token),
        )
        claude.update(changes)
        document.update(
            agents=[document["agents"][0], claude],
            dispatch_mode="queue",
            queue_runtime="external",
            external_worker_agent_ids=["codex", "claude"],
        )
        path.write_text(json.dumps(document))
        return load_hub_config(path)

    def test_explicit_choices_and_omitted_catalog_are_distinct(self):
        self.assertIsNone(self.load().require_agent("claude").model_catalog)
        configured = self.load(model_catalog=entries())
        self.assertEqual(configured.require_agent("claude").model_catalog, CATALOG)

    def test_invalid_bounds_fields_and_default_pair_fail_before_provider_invocation(self):
        first, second = entries()
        invalid = (
            None,
            {},
            [],
            [first] * 33,
            [first, first],
            [second],
            [{**first, "efforts": ["low"]}],
            [{**first, "model_id": "-example"}],
            [{**first, "model_id": "example\nmodel"}],
            [{**first, "model_id": "x" * 129}],
            [{**first, "label": ""}],
            [{**first, "label": "x" * 97}],
            [{**first, "label": "Example\u202elabel"}],
            [{**first, "efforts": []}],
            [{**first, "efforts": ["high", "high"]}],
            [{**first, "efforts": ["high", "ultracode"]}],
            [{**first, "efforts": ["high", True]}],
            [{**first, "extra": "example"}],
        )
        with patch("subprocess.run", side_effect=AssertionError("provider invocation")):
            for value in invalid:
                with (
                    self.subTest(value=value),
                    self.assertRaisesRegex(HubConfigError, "model_catalog"),
                ):
                    self.load(model_catalog=value)

    def test_other_runtime_cannot_claim_claude_catalog(self):
        with self.assertRaisesRegex(HubConfigError, "model_catalog"):
            self.load(runtime="opencode", model_catalog=entries())

    def test_maximum_catalog_keeps_each_requested_id_and_effort_order(self):
        models = (CATALOG[0],) + tuple(
            ProviderModel(f"example-model-{index}", f"Example {index}", ("max", "low"))
            for index in range(31)
        )
        self.assertEqual(
            self.load(model_catalog=entries(models)).require_agent("claude").model_catalog, models
        )


class ClaudeCatalogControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = continuity_fixtures.ClaudeServiceSelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.state = self.service.state
        self.topic = self.fixture.topic
        self.configure(CATALOG)
        self.cache = ProviderCatalogCache(
            self.service.config.state_path.with_name("provider-model-catalogs.json")
        )
        self.service.handle_update(self.update(1, "/agent claude"))

    @staticmethod
    def update(update_id, text):
        from tests.test_embedded_queue_service import update

        return update(update_id, text)

    def configure(self, models):
        self.service.config = replace(
            self.service.config,
            agents=tuple(
                replace(agent, model_catalog=models) if agent.runtime == "claude" else agent
                for agent in self.service.config.agents
            ),
        )

    def test_cold_warm_refresh_and_monitor_use_only_configuration(self):
        config = replace(
            self.service.config,
            agents=(self.service.config.require_agent("claude"),),
        )
        with (
            patch("subprocess.run", side_effect=AssertionError("subprocess")),
            patch("socket.socket", side_effect=AssertionError("network")),
            patch.object(
                self.service, "_discover_provider_models", side_effect=AssertionError("discovery")
            ),
            patch.object(
                self.service, "_source_version", side_effect=AssertionError("version probe")
            ),
            patch(
                "hermes_codex_router.catalog_refresh.native_codex_models",
                side_effect=AssertionError("Codex RPC"),
            ),
        ):
            for refresh in (False, False, True):
                snapshot = self.service._provider_catalog("claude", refresh=refresh)
                self.assertEqual(
                    tuple((item.model_id, item.label, item.efforts) for item in snapshot.models),
                    tuple((item.model_id, item.label, item.efforts) for item in CATALOG),
                )
            self.cache.request_refresh("claude")
            self.assertEqual(refresh_provider_catalogs(config).refreshed, ("claude",))
            self.assertEqual(refresh_provider_catalogs(config).refreshed, ())

    def test_fresh_old_cache_removed_model_and_removed_effort_are_invalid_immediately(self):
        snapshot = self.service._provider_catalog("claude")
        second = snapshot.models[1]
        current = self.state.active_session(self.topic.topic_id)
        self.configure((CATALOG[0],))
        message = parse_topic_message(self.update(2, "/model"))
        assert message is not None and current is not None
        with self.assertRaisesRegex(ValueError, "unavailable|available"):
            self.service._apply_model_selection(
                project=self.fixture.project,
                topic=self.topic,
                agent_id="claude",
                callback_key=second.callback_key,
                effort="medium",
                message=message,
                expected_session_id=current.session_id,
            )
        self.assertEqual(self.state.active_session(self.topic.topic_id), current)
        self.configure((CATALOG[0], replace(CATALOG[1], efforts=("low",))))
        with self.assertRaisesRegex(ValueError, "unavailable|available"):
            self.service._apply_model_selection(
                project=self.fixture.project,
                topic=self.topic,
                agent_id="claude",
                callback_key=second.callback_key,
                effort="medium",
                message=message,
                expected_session_id=current.session_id,
            )
        self.assertEqual(self.state.active_session(self.topic.topic_id), current)

    def test_monitor_reconciles_fresh_changed_config_and_preserves_other_provider(self):
        self.service._provider_catalog("claude")
        foreign = self.cache.store(
            "codex",
            (ProviderModel("example-codex", "Example", ("high",)),),
            source_version="example fixture",
        )
        self.configure((CATALOG[0],))
        config = replace(self.service.config, agents=(self.service.config.require_agent("claude"),))
        self.assertEqual(refresh_provider_catalogs(config).refreshed, ("claude",))
        self.assertEqual(self.cache.load("codex"), foreign)
        snapshot = self.cache.load("claude")
        assert snapshot is not None
        self.assertEqual(len(snapshot.models), 1)

    def test_omitted_catalog_preserves_single_default_display_and_monitor(self):
        self.configure(None)
        snapshot = self.service._provider_catalog("claude", refresh=True)
        self.assertEqual(
            [(model.model_id, model.efforts) for model in snapshot.models],
            [("example-default", ("high",))],
        )
        config = replace(self.service.config, agents=(self.service.config.require_agent("claude"),))
        self.cache.request_refresh("claude")
        self.assertEqual(refresh_provider_catalogs(config).refreshed, ("claude",))
        self.assertEqual(self.service._cached_provider_catalog("claude").models, snapshot.models)

    def test_relabel_reorder_and_removed_active_effort_reconcile_without_reset(self):
        snapshot = self.service._provider_catalog("claude")
        current = self.state.active_session(self.topic.topic_id)
        assert current is not None
        self.state.replace_active_session(
            self.topic.topic_id,
            model="example-second",
            effort="medium",
            runtime="claude",
            expected_session_id=current.session_id,
        )
        current = self.state.active_session(self.topic.topic_id)
        self.configure((replace(CATALOG[1], label="Renamed example", efforts=("low",)), CATALOG[0]))
        updated = self.service._provider_catalog("claude")
        self.assertEqual(updated.models[0].callback_key, snapshot.models[1].callback_key)
        self.assertEqual(updated.models[0].label, "Renamed example")
        decision = self.service._command_orchestrator().model_menu(self.topic, "claude", updated)
        self.assertIn("example-second · medium (outside", decision.html)
        self.assertEqual(self.state.active_session(self.topic.topic_id), current)

    def test_forged_cache_cannot_authorize_unconfigured_pair(self):
        snapshot = self.cache.store(
            "claude",
            (ProviderModel("example-foreign", "Example foreign", ("high",)),),
            source_version="configured Claude choices; availability unverified",
        )
        current = self.state.active_session(self.topic.topic_id)
        message = parse_topic_message(self.update(2, "/model"))
        assert current is not None and message is not None
        with self.assertRaisesRegex(ValueError, "available"):
            self.service._apply_model_selection(
                project=self.fixture.project,
                topic=self.topic,
                agent_id="claude",
                callback_key=snapshot.models[0].callback_key,
                effort="high",
                message=message,
                expected_session_id=current.session_id,
            )
        self.assertEqual(self.state.active_session(self.topic.topic_id), current)

    def test_concurrent_old_monitor_snapshot_cannot_restore_removed_choice(self):
        snapshot = self.service._provider_catalog("claude")
        self.configure((CATALOG[0],))
        current = self.state.active_session(self.topic.topic_id)
        with patch.object(ProviderCatalogCache, "store", return_value=snapshot):
            with self.assertRaisesRegex(ProviderCatalogError, "cache reconciliation"):
                self.service._cached_provider_catalog("claude")
        self.assertEqual(self.state.active_session(self.topic.topic_id), current)

    def test_monitor_catalog_race_isolated_from_next_provider(self):
        stale = self.service._provider_catalog("claude")
        self.configure((CATALOG[0],))
        config = replace(
            self.service.config,
            agents=(
                self.service.config.require_agent("claude"),
                self.service.config.require_agent("codex"),
            ),
        )
        real_store = ProviderCatalogCache.store

        def raced_store(cache, agent_id, models, **kwargs):
            if agent_id == "claude":
                return stale
            return real_store(cache, agent_id, models, **kwargs)

        with (
            patch.object(ProviderCatalogCache, "store", new=raced_store),
            patch(
                "hermes_codex_router.catalog_refresh.native_codex_models",
                return_value=(ProviderModel("example-native", "Example native", ("high",)),),
            ) as native,
        ):
            result = refresh_provider_catalogs(config)
        self.assertEqual(result.failed, ("claude",))
        self.assertEqual(result.refreshed, ("codex",))
        native.assert_called_once_with(config)
        self.assertEqual(self.cache.load("claude"), stale)

    def test_real_refresh_callback_reports_local_projection_without_monitor_queue(self):
        from hermes_codex_router.session_controls import bind_controls

        real_run = subprocess.run

        def root_validation_only(argv, **kwargs):
            self.assertEqual(
                argv,
                ("git", "-C", str(self.fixture.project.root), "rev-parse", "--show-toplevel"),
            )
            return real_run(argv, **kwargs)

        self.service.config = replace(
            self.service.config,
            queue_runtime="external",
            external_worker_agent_ids=("codex", "claude"),
        )
        callback = bind_controls(
            self.state, self.topic.topic_id, [("Refresh", "modelrefresh:claude:0")]
        )[0][1]
        with (
            patch.object(self.service.telegram, "answer_callback") as answer,
            patch.object(
                self.service, "_discover_provider_models", side_effect=AssertionError("discovery")
            ),
            patch("subprocess.run", side_effect=root_validation_only),
        ):
            self.assertTrue(
                self.service.handle_update(
                    {
                        "update_id": 2,
                        "callback_query": {
                            "id": "example-refresh",
                            "from": {"id": 42},
                            "data": callback,
                            "message": {
                                "message_id": 101,
                                "chat": {"id": -1001234567890, "type": "supergroup"},
                                "message_thread_id": 77,
                            },
                        },
                    }
                )
            )
        answer.assert_any_call("example-refresh", "Refreshing configured choices…")
        self.assertFalse(self.cache.is_stale("claude"))

    def test_real_callback_selects_second_model_without_changing_native_binding(self):
        from hermes_codex_router.session_controls import bind_controls

        previous = self.state.active_session(self.topic.topic_id)
        assert previous is not None
        before = dict(
            self.state._connection.execute(
                "SELECT * FROM agent_sessions WHERE session_id=?", (previous.session_id,)
            ).fetchone()
        )
        snapshot = self.service._provider_catalog("claude")
        key = snapshot.models[1].callback_key
        callback = bind_controls(
            self.state, self.topic.topic_id, [("Medium", f"use:claude:{key}:medium")]
        )[0][1]
        handled = self.service.handle_update(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "example-choice",
                    "from": {"id": 42},
                    "data": callback,
                    "message": {
                        "message_id": 101,
                        "chat": {"id": -1001234567890, "type": "supergroup"},
                        "message_thread_id": 77,
                    },
                },
            }
        )
        self.assertTrue(handled)
        after = dict(
            self.state._connection.execute(
                "SELECT * FROM agent_sessions WHERE session_id=?", (previous.session_id,)
            ).fetchone()
        )
        self.assertEqual((after["model"], after["effort"]), ("example-second", "medium"))
        for name in ("model", "effort", "updated_at"):
            before.pop(name)
            after.pop(name)
        self.assertEqual(after, before)
        self.assertEqual(self.fixture.client.started_threads, 0)
        self.assertEqual(self.fixture.client.turn_threads, [])

    def test_removed_active_choice_is_shown_but_not_reset_or_authorized(self):
        previous = self.state.active_session(self.topic.topic_id)
        assert previous is not None
        snapshot = self.service._provider_catalog("claude")
        decision = self.service._command_orchestrator().model_menu(self.topic, "claude", snapshot)
        self.assertIn("configured", decision.html.lower())
        self.assertIn("unverified", decision.html.lower())
        self.assertIn("example-saved", decision.html)
        self.assertIn("outside", decision.html.lower())
        self.assertEqual(self.state.active_session(self.topic.topic_id), previous)
