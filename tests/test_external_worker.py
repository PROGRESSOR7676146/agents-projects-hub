from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.external_runtime import (
    ExternalTurnResult,
    ProviderLimitError,
    ProviderUnavailableError,
)
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.provider_limits import ProviderLimit
from hermes_codex_router.service import ProjectHubService, QueueAcceptanceError, ServiceError
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TopicMessage


class Adapter:
    def __init__(
        self,
        runtime: str,
        *,
        limit: bool = False,
        unavailable: bool = False,
        session_id: bool = True,
    ) -> None:
        self.runtime = runtime
        self.limit = limit
        self.unavailable = unavailable
        self.session_id = session_id
        self.calls = 0

    def run_turn(self, **_kwargs: object) -> ExternalTurnResult:
        self.calls += 1
        if self.limit:
            raise ProviderLimitError(ProviderLimit(self.runtime, "weekly", 0, 1))
        if self.unavailable:
            raise ProviderUnavailableError(
                "unsupported_network_location",
                "Antigravity is unavailable from the computer's current network location.",
            )
        return ExternalTurnResult(
            self.runtime,
            f"{self.runtime} answer",
            f"{self.runtime}-1" if self.session_id else None,
            "model-1",
        )


class ExternalQueueWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        root = base / "project"
        (root / ".git").mkdir(parents=True)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("example-project", -1001234567890),),
            agents=(
                AgentDefinition(
                    "opencode",
                    "OpenCode",
                    "example_open_bot",
                    "opencode",
                    None,
                    False,
                    False,
                    "provider-selected",
                    "high",
                    executable="opencode",
                ),
                AgentDefinition(
                    "antigravity",
                    "Antigravity",
                    "example_agy_bot",
                    "antigravity",
                    None,
                    False,
                    False,
                    "provider-selected",
                    "high",
                    executable="agy",
                ),
            ),
            dispatch_mode="queue",
            queue_runtime="external",
            external_worker_agent_ids=("opencode", "antigravity"),
        )
        self.registry = ProjectRegistry(
            1, (base,), (Project("example-project", "Example", "Example", root),)
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def enqueue(self, agent_id: str, message_id: int) -> str:
        state = HubState.open(self.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=70 + message_id,
                title="Example",
            )
            agent = self.config.require_agent(agent_id)
            session = state.activate_agent(
                topic.topic_id, agent_id, agent.default_model, agent.default_effort
            )
            job, _ = state.enqueue_provider_job(
                idempotency_key=f"telegram:-1001234567890:{message_id}",
                chat_id=-1001234567890,
                message_id=message_id,
                topic_id=topic.topic_id,
                agent_id=agent_id,
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="durable task",
                context_watermark=None,
                handoff_id=None,
            )
            return job.job_id
        finally:
            state.close()

    def worker(self, agent_id: str, adapter: Adapter) -> ExternalQueueWorker:
        return ExternalQueueWorker(
            self.config,
            agent_id,
            registry=self.registry,
            adapter=cast(Any, adapter),
            worker_id=f"test-{agent_id}",
        )

    def test_opencode_and_antigravity_workers_are_independent_and_have_no_telegram(self) -> None:
        open_job = self.enqueue("opencode", 1)
        agy_job = self.enqueue("antigravity", 2)
        opencode_adapter = Adapter("opencode", limit=True)
        antigravity_adapter = Adapter("antigravity")
        opencode = self.worker("opencode", opencode_adapter)
        antigravity = self.worker("antigravity", antigravity_adapter)
        try:
            self.assertFalse(hasattr(opencode, "telegram"))
            self.assertFalse(hasattr(antigravity, "telegram"))
            self.assertTrue(opencode.run_cycle())
            self.assertTrue(antigravity.run_cycle())
            self.assertEqual(opencode.state.get_provider_job(open_job).status, "failed")
            self.assertIn(
                "limit reached",
                opencode.state.get_telegram_outbox_for_job(open_job).telegram_html,
            )
            self.assertEqual(antigravity.state.get_provider_job(agy_job).status, "result_ready")
            open_health = opencode.state.get_runtime_health("provider_worker", "test-opencode")
            agy_health = antigravity.state.get_runtime_health("provider_worker", "test-antigravity")
            assert open_health is not None
            assert agy_health is not None
            self.assertEqual(
                (open_health.provider_state, open_health.quota_remaining_percent),
                ("limited", 0.0),
            )
            self.assertEqual(open_health.error_code, "provider_limit")
            self.assertEqual(agy_health.provider_state, "ready")
            self.assertIsNotNone(agy_health.success_at)
            self.assertEqual(agy_health.activity_state, "idle")
            self.assertIsNone(agy_health.active_job_id)
            events = cast(
                list[dict[str, object]], antigravity.state.status_snapshot()["runtime_events"]
            )
            self.assertTrue(any(event["code"] == "provider_limit" for event in events))
        finally:
            opencode.close()
            antigravity.close()

    def test_known_provider_unavailability_fails_with_a_visible_notice(self) -> None:
        job_id = self.enqueue("antigravity", 31)
        worker = self.worker("antigravity", Adapter("antigravity", unavailable=True))
        try:
            self.assertTrue(worker.run_cycle())
            job = worker.state.get_provider_job(job_id)
            self.assertEqual((job.status, job.error_class), ("failed", "provider_unavailable"))
            outbox = worker.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(outbox.status, "pending")
            self.assertIn("current network location", outbox.telegram_html)
            leased = worker.state.lease_telegram_outbox("antigravity", "test-sender")
            assert leased is not None and leased.lease_token is not None
            worker.state.mark_telegram_outbox_delivered(
                leased.outbox_id,
                leased.lease_token,
                telegram_message_id=123,
            )
            self.assertEqual(worker.state.get_provider_job(job_id).status, "failed")
        finally:
            worker.close()

    def test_stop_after_provider_lease_returns_unstarted_job_to_queue(self) -> None:
        job_id = self.enqueue("opencode", 30)
        worker = self.worker("opencode", Adapter("opencode"))
        original_lease = worker.state.lease_provider_job

        def lease_then_stop(*args: object, **kwargs: object) -> object:
            leased = cast(Any, original_lease)(*args, **kwargs)
            worker.stop()
            return leased

        worker.state.lease_provider_job = lease_then_stop  # type: ignore[method-assign]
        try:
            self.assertFalse(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(job_id).status, "queued")
        finally:
            worker.close()

    def test_hand_built_config_cannot_make_an_externally_managed_agent_a_worker(self) -> None:
        externally_managed = replace(
            self.config,
            agents=(
                replace(self.config.require_agent("opencode"), managed_externally=True),
                self.config.require_agent("antigravity"),
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "locally managed"):
            ExternalQueueWorker(externally_managed, "opencode", registry=self.registry)

    def test_restart_recovers_only_its_provider_lease(self) -> None:
        open_job = self.enqueue("opencode", 3)
        agy_job = self.enqueue("antigravity", 4)
        first = self.worker("opencode", Adapter("opencode"))
        try:
            leased = first.state.lease_provider_job("opencode", "lost-worker", lease_seconds=1)
            assert leased is not None
            first.state.heartbeat_provider_job(
                open_job,
                leased.lease_token or "",
                lease_seconds=1,
                now=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
        finally:
            first.close()
        resumed = self.worker("opencode", Adapter("opencode"))
        try:
            self.assertTrue(resumed.run_cycle())
            self.assertEqual(resumed.state.get_provider_job(open_job).status, "result_ready")
            self.assertEqual(resumed.state.get_provider_job(agy_job).status, "queued")
        finally:
            resumed.close()

    def test_first_external_turn_without_provider_session_is_indeterminate(self) -> None:
        job_id = self.enqueue("opencode", 6)
        worker = self.worker("opencode", Adapter("opencode", session_id=False))
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
            self.assertIn(
                "did not retry",
                worker.state.get_telegram_outbox_for_job(job_id).telegram_html,
            )
        finally:
            worker.close()

    def test_controller_skips_isolated_adapter_but_keeps_nonisolated_embedded_provider(
        self,
    ) -> None:
        isolated_job = self.enqueue("opencode", 5)
        nonisolated = AgentDefinition(
            "other-open",
            "Other Open",
            "example_other_bot",
            "opencode",
            None,
            False,
            False,
            "provider-selected",
            "high",
            executable="opencode",
        )
        config = replace(
            self.config,
            agents=self.config.agents + (nonisolated,),
        )
        state = HubState.open(config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=99,
                title="Other",
            )
            session = state.activate_agent(
                topic.topic_id, "other-open", "provider-selected", "high"
            )
            state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:other",
                chat_id=-1001234567890,
                message_id=99,
                topic_id=topic.topic_id,
                agent_id="other-open",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="embedded task",
                context_watermark=None,
                handoff_id=None,
            )
        finally:
            state.close()

        isolated_adapter = Adapter("opencode")
        embedded_adapter = Adapter("opencode")

        class Sender:
            def send_html(self, *_args: object, **_kwargs: object) -> int:
                return 1

        class External:
            def __init__(self, adapter: Adapter) -> None:
                self.adapter = adapter
                self.telegram = Sender()

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = self.registry
        controller.telegram = Sender()
        controller.external_services = {
            "opencode": External(isolated_adapter),
            "other-open": External(embedded_adapter),
        }
        controller._codex_client = None
        self.assertTrue(controller.run_embedded_queue_cycle())
        self.assertEqual(isolated_adapter.calls, 0)
        self.assertEqual(embedded_adapter.calls, 1)
        verification = HubState.open(config.state_path)
        try:
            self.assertEqual(verification.get_provider_job(isolated_job).status, "queued")
        finally:
            verification.close()

    def test_isolated_provider_ingress_enqueues_without_calling_its_adapter(self) -> None:
        codex = AgentDefinition(
            "codex", "Codex", "example_codex_bot", "codex", None, True, False, "gpt-5.6-sol", "high"
        )
        config = replace(self.config, agents=(codex,) + self.config.agents)

        class Sender:
            def __init__(self) -> None:
                self.sent: list[str] = []

            def send_html(self, *_args: object, **_kwargs: object) -> int:
                self.sent.append("sent")
                return len(self.sent)

        class ForbiddenExternal:
            class Adapter:
                def run_turn(self, **_kwargs: object) -> ExternalTurnResult:
                    raise AssertionError("controller invoked isolated provider adapter")

            def __init__(self) -> None:
                self.adapter = self.Adapter()
                self.telegram = Sender()

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = self.registry
        controller.state = HubState.open(config.state_path)
        controller.agent = codex
        controller.telegram = Sender()
        controller.usernames = {agent.agent_id: agent.telegram_username for agent in config.agents}
        controller.external_services = {"opencode": ForbiddenExternal()}
        controller._codex_client = None
        try:
            self.assertTrue(
                controller.handle_update(
                    {
                        "update_id": 7,
                        "message": {
                            "message_id": 7,
                            "message_thread_id": 107,
                            "is_topic_message": True,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {
                                "id": -1001234567890,
                                "type": "supergroup",
                                "title": "Example",
                            },
                            "text": "@example_open_bot queued only",
                        },
                    }
                )
            )
            topic = controller.state.find_topic(-1001234567890, 107)
            assert topic is not None
            jobs = controller.state.provider_jobs_for_topic(topic.topic_id)
            self.assertEqual([(job.agent_id, job.status) for job in jobs], [("opencode", "queued")])
        finally:
            controller.state.close()

    def test_managed_external_provider_is_never_admitted_to_local_queue(self) -> None:
        codex = AgentDefinition(
            "codex",
            "Codex",
            "example_codex_bot",
            "codex",
            None,
            True,
            False,
            "gpt-5.6-sol",
            "high",
        )
        external = AgentDefinition(
            "hermes",
            "Hermes",
            "example_hermes_bot",
            "hermes",
            None,
            False,
            True,
            "provider-selected",
            "high",
        )
        config = replace(self.config, agents=(codex,) + self.config.agents + (external,))

        class Sender:
            def send_html(self, *_args: object, **_kwargs: object) -> int:
                raise AssertionError("managed external routing must not send as Hub")

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = self.registry
        controller.state = HubState.open(config.state_path)
        controller.agent = codex
        controller.telegram = Sender()
        controller.usernames = {agent.agent_id: agent.telegram_username for agent in config.agents}
        controller.external_services = {}
        controller._codex_client = None
        try:
            self.assertFalse(controller._queue_enabled("hermes"))
            self.assertFalse(controller._embedded_consumer_owns_agent("hermes"))
            self.assertFalse(
                controller.handle_update(
                    {
                        "update_id": 8,
                        "message": {
                            "message_id": 8,
                            "message_thread_id": 108,
                            "is_topic_message": True,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {
                                "id": -1001234567890,
                                "type": "supergroup",
                                "title": "Example",
                            },
                            "text": "@example_hermes_bot native gateway request",
                        },
                    }
                )
            )
            topic = controller.state.find_topic(-1001234567890, 108)
            assert topic is not None
            self.assertEqual(controller.state.provider_jobs_for_topic(topic.topic_id), ())
            self.assertTrue(
                controller.state.claim_message(-1001234567890, 8, observer_agent_id="claim-audit")
            )
            session = controller.state.activate_agent(
                topic.topic_id, "hermes", "provider-selected", "high"
            )
            with self.assertRaisesRegex(QueueAcceptanceError, "native gateway"):
                controller._enqueue_provider_turn(
                    message=TopicMessage(
                        80,
                        80,
                        -1001234567890,
                        108,
                        "Example",
                        42,
                        "must not enqueue",
                    ),
                    topic=topic,
                    session=session,
                    prompt="must not enqueue",
                    context_watermark=None,
                    handoff_id=None,
                )
            self.assertEqual(controller.state.provider_jobs_for_topic(topic.topic_id), ())
        finally:
            controller.state.close()

    def test_managed_external_routes_are_partitioned_before_local_admission(self) -> None:
        codex = AgentDefinition(
            "codex",
            "Codex",
            "example_codex_bot",
            "codex",
            None,
            True,
            False,
            "gpt-5.6-sol",
            "high",
        )
        hermes = AgentDefinition(
            "hermes",
            "Hermes",
            "example_hermes_bot",
            "hermes",
            None,
            False,
            True,
            "provider-selected",
            "high",
        )
        native = AgentDefinition(
            "native",
            "Native",
            "example_native_bot",
            "hermes",
            None,
            False,
            True,
            "provider-selected",
            "high",
        )
        config = replace(self.config, agents=(codex,) + self.config.agents + (hermes, native))

        class Sender:
            def send_html(self, *_args: object, **_kwargs: object) -> int:
                raise AssertionError("externally managed traffic must not be answered by Hub")

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = self.registry
        controller.state = HubState.open(config.state_path)
        controller.agent = codex
        controller.telegram = Sender()
        controller.usernames = {agent.agent_id: agent.telegram_username for agent in config.agents}
        controller.external_services = {}
        controller._codex_client = None

        def incoming(message_id: int, thread_id: int, text: str) -> dict[str, object]:
            return {
                "update_id": message_id,
                "message": {
                    "message_id": message_id,
                    "message_thread_id": thread_id,
                    "is_topic_message": True,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": -1001234567890, "type": "supergroup", "title": "Example"},
                    "text": text,
                },
            }

        try:
            active_topic = controller.state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=109,
                title="Example",
            )
            controller.state.activate_agent(
                active_topic.topic_id, "hermes", "provider-selected", "high"
            )
            self.assertFalse(controller.handle_update(incoming(9, 109, "native active request")))
            self.assertTrue(
                controller.state.claim_message(-1001234567890, 9, observer_agent_id="audit")
            )

            reply = incoming(10, 110, "native reply")
            cast(dict[str, Any], reply["message"])["reply_to_message"] = {
                "message_id": 90,
                "from": {"id": 90, "is_bot": True, "username": "example_hermes_bot"},
            }
            self.assertFalse(controller.handle_update(reply))
            self.assertTrue(
                controller.state.claim_message(-1001234567890, 10, observer_agent_id="audit")
            )

            self.assertFalse(
                controller.handle_update(
                    incoming(11, 111, "@example_hermes_bot @example_native_bot native only")
                )
            )
            self.assertTrue(
                controller.state.claim_message(-1001234567890, 11, observer_agent_id="audit")
            )

            self.assertTrue(
                controller.handle_update(
                    incoming(12, 112, "@example_hermes_bot @example_open_bot mixed request")
                )
            )
            mixed_topic = controller.state.find_topic(-1001234567890, 112)
            assert mixed_topic is not None
            self.assertEqual(
                [
                    job.agent_id
                    for job in controller.state.provider_jobs_for_topic(mixed_topic.topic_id)
                ],
                ["opencode"],
            )

            codex_topic = controller.state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=114,
                title="Example",
            )
            controller.state.activate_agent(
                codex_topic.topic_id,
                "antigravity",
                "provider-selected",
                "high",
            )
            self.assertTrue(
                controller.handle_update(
                    incoming(14, 114, "@example_codex_bot answer as a satellite")
                )
            )
            active_after = controller.state.active_session(codex_topic.topic_id)
            assert active_after is not None
            self.assertEqual(active_after.agent_id, "antigravity")
            self.assertEqual(
                [
                    job.agent_id
                    for job in controller.state.provider_jobs_for_topic(codex_topic.topic_id)
                ],
                ["codex"],
            )
        finally:
            controller.state.close()

    def test_managed_external_catalog_refresh_never_invokes_local_runtime(self) -> None:
        external = AgentDefinition(
            "hermes",
            "Hermes",
            "example_hermes_bot",
            "hermes",
            None,
            False,
            True,
            "provider-selected",
            "high",
        )
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = replace(self.config, agents=self.config.agents + (external,))
        with patch.object(
            controller, "_discover_provider_models", side_effect=AssertionError("CLI invoked")
        ):
            cold = controller._provider_catalog("hermes", refresh=True)
            warm = controller._provider_catalog("hermes", refresh=False)
        self.assertEqual(cold.source_version, "externally managed fallback")
        self.assertEqual(warm.models[0].model_id, "provider-selected")

    def test_controller_fails_fast_for_legacy_managed_external_jobs(self) -> None:
        token = Path(self.tempdir.name) / "codex-token"
        token.write_text("123456:secret-token-value", encoding="utf-8")
        token.chmod(0o600)
        project_root = self.registry.require_project("example-project").root
        self.config.registry_path.write_text(
            '{"schema_version": 1, "allowed_roots": ["%s"], "projects": '
            '[{"project_id": "example-project", "display_name": "Example", '
            '"topic_name": "Example", "root": "%s"}]}' % (project_root.parent, project_root),
            encoding="utf-8",
        )
        codex = AgentDefinition(
            "codex",
            "Codex",
            "example_codex_bot",
            "codex",
            token,
            True,
            False,
            "gpt-5.6-sol",
            "high",
        )
        hermes = AgentDefinition(
            "hermes",
            "Hermes",
            "example_hermes_bot",
            "hermes",
            None,
            False,
            True,
            "provider-selected",
            "high",
        )
        config = replace(self.config, agents=(codex, hermes))
        state = HubState.open(config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=113,
                title="Example",
            )
            session = state.activate_agent(topic.topic_id, "hermes", "provider-selected", "high")
            state.enqueue_provider_job(
                idempotency_key="legacy-managed-external",
                chat_id=-1001234567890,
                message_id=13,
                topic_id=topic.topic_id,
                agent_id="hermes",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="accepted before ownership changed",
            )
        finally:
            state.close()
        with self.assertRaisesRegex(ServiceError, "drain or explicitly reconcile"):
            ProjectHubService(config)
        direct_service = ProjectHubService(config, direct_messages_only=True)
        direct_service.close()

    def test_cli_selects_one_configured_external_worker_and_rejects_unknown_agent(self) -> None:
        config_path = Path(self.tempdir.name) / "hub.json"
        self.config.registry_path.write_text(
            '{"schema_version": 1, "allowed_roots": [], "projects": []}', encoding="utf-8"
        )
        config_path.write_text(
            """{
              "schema_version": 1,
              "owner_user_ids": [42],
              "registry_path": "%s",
              "state_path": "%s",
              "projects": [{"project_id": "example-project", "telegram_chat_id": -1001234567890}],
              "dispatch_mode": "queue",
              "queue_runtime": "external",
              "external_worker_agent_ids": ["opencode"],
              "agents": [{"agent_id": "opencode", "display_name": "OpenCode", "telegram_username": "example_open_bot", "runtime": "opencode", "token_file": "%s", "terminal_enabled": false}]
            }"""
            % (
                self.config.registry_path,
                self.config.state_path,
                Path(self.tempdir.name) / "token",
            ),
            encoding="utf-8",
        )

        class FakeWorker:
            instance: "FakeWorker | None" = None

            def __init__(self, _config: HubConfig, agent_id: str) -> None:
                self.agent_id = agent_id
                self.closed = False
                FakeWorker.instance = self

            def run_forever(self, *, poll_seconds: float) -> None:
                self.poll_seconds = poll_seconds

            def close(self) -> None:
                self.closed = True

        with patch("hermes_codex_router.cli.ExternalQueueWorker", FakeWorker):
            self.assertEqual(main(["worker", str(config_path), "--agent", "opencode"]), 0)
        assert FakeWorker.instance is not None
        self.assertEqual(FakeWorker.instance.agent_id, "opencode")
        self.assertTrue(FakeWorker.instance.closed)
        self.assertEqual(main(["worker", str(config_path), "--agent", "missing"]), 2)
