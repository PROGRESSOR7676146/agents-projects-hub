from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any, cast
from unittest.mock import Mock

import test_codex_worker as worker_fixtures
import test_service_integration as service_fixtures

from hermes_codex_router.provider_catalog import ProviderModel
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.session_adoption_state import AdoptionRequest, CodexSessionOrigins
from hermes_codex_router.state import HubState, StateError


class SessionAdoptionIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.service = cast(Any, ProjectHubService.__new__(ProjectHubService))
        self.service.config = replace(fixture.config, outbox_runtime="external")
        self.service.registry = fixture.registry
        self.service.state = HubState.open(fixture.config.state_path)
        self.addCleanup(self.service.state.close)
        self.service.agent = fixture.config.agents[0]
        self.service.telegram = service_fixtures.FakeTelegram()
        self.service.usernames = {"codex": self.service.agent.telegram_username}
        self.service._codex_client = None
        self.service._catalog_cache().store(
            "codex",
            (ProviderModel("gpt-5.6-sol", "Example model", ("high", "medium")),),
            source_version="fictional",
        )
        self.topic = self.service.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
        )
        self.old = self.service.state.activate_agent(
            self.topic.topic_id, "codex", "gpt-5.6-sol", "high"
        )
        self.origins = CodexSessionOrigins(self.service.state)
        self.session = self.origins.attach(
            AdoptionRequest(
                "example-project",
                self.topic.chat_id,
                77,
                "example-cli-thread",
                fixture.registry.projects[0].root,
                "gpt-5.6-sol",
                "high",
                self.old.session_id,
            ),
            expected_session_id=self.old.session_id,
        ).session

    def send(self, message_id, text):
        return self.service.handle_update(service_fixtures.update(message_id, text))

    def test_return_then_old_input_is_rejected_with_receipt_and_new_input_admitted(self) -> None:
        self.send(100, "/return")
        self.assertEqual(self.origins.require(self.session.session_id).activation_message_id, 100)
        for message_id, text in ((20, "Delayed old request"), (21, "/context")):
            self.assertTrue(self.send(message_id, text))
            self.assertIsNotNone(
                self.service.state._connection.execute(
                    "SELECT 1 FROM observed_messages WHERE message_id=?", (message_id,)
                ).fetchone()
            )
        self.assertEqual(
            self.service.state._connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[
                0
            ],
            0,
        )
        self.send(101, "Fresh request")
        self.assertEqual(
            self.service.state._connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[
                0
            ],
            1,
        )

    def test_old_reset_and_model_callbacks_cannot_change_new_generation(self) -> None:
        self.send(100, "/return")
        self.send(101, "/model")
        # An old pre-adoption effort menu has a still-valid catalog key, but no
        # current-session provenance. Numeric message age is not the authority.
        catalog = self.service._provider_catalog("codex", refresh=False)
        key = catalog.models[0].callback_key
        for index, data in enumerate(
            (f"new:confirm:{self.old.session_id}", f"use:codex:{key}:medium")
        ):
            self.service.handle_update(
                service_fixtures.callback(1000 + index, f"old-{index}", data)
            )
        self.assertEqual(
            self.service.state.active_session(self.topic.topic_id).session_id,
            self.session.session_id,
        )
        self.assertEqual(self.service.state.active_session(self.topic.topic_id).effort, "high")

    def test_fresh_controls_are_bound_and_model_change_keeps_adopted_thread(self) -> None:
        self.send(100, "/return")
        self.send(101, "/model")
        provider = service_fixtures.callback_values(self.service.telegram.markups[-1])[0]
        self.service.handle_update(service_fixtures.callback(102, "provider", provider))
        model = service_fixtures.callback_values(self.service.telegram.markups[-1])[0]
        self.service.handle_update(service_fixtures.callback(103, "model", model))
        values = service_fixtures.callback_values(self.service.telegram.markups[-1])
        self.assertTrue(all(len(value.encode()) <= 64 for value in values))
        medium = next(value for value in values if ":medium" in value)
        self.service.handle_update(service_fixtures.callback(104, "effort", medium))
        current = self.service.state.active_session(self.topic.topic_id)
        self.assertEqual(current.session_id, self.session.session_id)
        self.assertEqual(current.provider_session_id, "example-cli-thread")
        self.assertEqual(current.effort, "medium")

    def test_batch_append_checks_activation_boundary_in_its_transaction(self) -> None:
        self.send(100, "/return")
        params = dict(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            provider_session_id=self.session.provider_session_id,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Fresh",
            appended_user_text="Fresh",
            quiet_ms=5000,
            max_ms=10000,
        )
        self.service.state.enqueue_or_append_provider_job(
            idempotency_key="fresh", message_id=101, **params
        )
        with self.assertRaisesRegex(StateError, "activation"):
            self.service.state.enqueue_or_append_provider_job(
                idempotency_key="old", message_id=90, **params
            )

    def test_control_menu_return_requires_real_message_for_first_boundary(self) -> None:
        self.send(90, "/menu")
        values = service_fixtures.callback_values(self.service.telegram.markups[-1])
        self.assertTrue(all("~" in value for value in values))
        value = next(value for value in values if value.startswith("menu:return"))
        self.service.handle_update(service_fixtures.callback(91, "return-button", value))
        self.assertIsNone(self.origins.require(self.session.session_id).activation_message_id)
        self.assertEqual(
            self.service.state.get_session(self.session.session_id).writer_mode, "local"
        )
        self.send(100, "/return")
        self.assertEqual(self.origins.require(self.session.session_id).activation_message_id, 100)

    def test_direct_inline_path_refuses_origin_even_with_external_config(self) -> None:
        self.service._client = Mock(side_effect=AssertionError("must reject before client"))
        with self.assertRaisesRegex(ValueError, "adopted"):
            self.service._run_codex_turn(
                project=None, topic=self.topic, session=self.session, text="Example", message=None
            )
        self.service._client.assert_not_called()
