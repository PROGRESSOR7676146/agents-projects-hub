from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import external_worker as external_worker_module
from hermes_codex_router.cli import main
from hermes_codex_router.external_runtime import (
    ExternalTurnInterrupted,
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
from hermes_codex_router.project_editing import ProjectEditStore
from hermes_codex_router.provider_limits import ProviderLimit
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.service import ProjectHubService, QueueAcceptanceError, ServiceError
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TopicMessage
from tests.delivery_fixture import complete_final_delivery
from tests.git_fixtures import init_git_root
from tests.stop_fixtures import pending_stop


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
        self.generate_artifact = False
        self.calls = 0
        self.last_prompt = ""
        self.last_cwd: Path | None = None
        self.last_session_id: str | None = None

    def run_turn(self, **kwargs: object) -> ExternalTurnResult:
        self.calls += 1
        self.last_prompt = str(kwargs.get("prompt") or "")
        self.last_cwd = cast(Path | None, kwargs.get("cwd"))
        if self.limit:
            raise ProviderLimitError(ProviderLimit(self.runtime, "weekly", 0, 1))
        if self.unavailable:
            raise ProviderUnavailableError(
                "unsupported_network_location",
                "Antigravity is unavailable from the computer's current network location.",
            )
        if self.generate_artifact and kwargs.get("staging_dir"):
            staging = Path(str(kwargs["staging_dir"]))
            (staging / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-data")
        if self.runtime == "claude" and self.session_id:
            native_id = kwargs.get("new_session_id") or kwargs.get("session_id")
            assert isinstance(native_id, str)
            self.last_session_id = native_id
        return ExternalTurnResult(
            self.runtime,
            f"{self.runtime} answer",
            (self.last_session_id if self.runtime == "claude" else f"{self.runtime}-1")
            if self.session_id
            else None,
            "model-1",
        )


class InterruptibleAdapter(Adapter):
    def __init__(self, runtime: str) -> None:
        super().__init__(runtime)
        self.interrupted = threading.Event()

    def run_turn(self, **kwargs: object) -> ExternalTurnResult:
        # Long enough for the worker's stop monitor to poll several times.
        self.interrupted.wait(0.6)
        return super().run_turn(**kwargs)

    def interrupt(self) -> None:
        self.interrupted.set()


class ExternalQueueWorkerTests(unittest.TestCase):
    def test_idle_codex_worker_discards_cached_fallback_after_socket_recovery(self) -> None:
        class OldClient:
            closed = False

            def close(self) -> None:
                self.closed = True

        class Supervisor:
            recovered = False

            def restore_socket_at_idle(self) -> bool:
                return self.recovered

        worker = cast(Any, ExternalQueueWorker.__new__(ExternalQueueWorker))
        worker.supervisor = Supervisor()
        old = OldClient()
        worker._codex_client = old
        worker._restore_codex_socket_at_idle()
        self.assertFalse(old.closed)
        self.assertIs(worker._codex_client, old)

        worker.supervisor.recovered = True
        worker._restore_codex_socket_at_idle()
        self.assertTrue(old.closed)
        self.assertIsNone(worker._codex_client)

    def test_worker_health_names_missing_human_approval_transport(self) -> None:
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
        config = replace(
            self.config,
            agents=(codex, *self.config.agents),
            external_worker_agent_ids=("codex", *self.config.external_worker_agent_ids),
        )
        worker = ExternalQueueWorker(config, "codex", registry=self.registry)
        self.addCleanup(worker.close)
        assert worker.supervisor is not None
        worker.supervisor.transport_mode = "stdio-fallback"
        worker._publish_health()
        health = worker.state.get_runtime_health("provider_worker", worker.worker_id)
        assert health is not None
        self.assertEqual(health.error_code, "codex_approvals_unavailable")

        worker.supervisor.transport_mode = "socket"
        with patch.object(Path, "is_socket", return_value=True):
            worker._publish_health()
        health = worker.state.get_runtime_health("provider_worker", worker.worker_id)
        assert health is not None
        self.assertIsNone(health.error_code)

        # Before any turn: an invisible shared socket is already approval loss.
        worker.supervisor.transport_mode = None
        with patch.object(Path, "is_socket", return_value=False):
            worker._publish_health()
        health = worker.state.get_runtime_health("provider_worker", worker.worker_id)
        assert health is not None
        self.assertEqual(health.error_code, "codex_approvals_unavailable")

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        root = base / "project"
        init_git_root(root)
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
        self.config.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(base)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example",
                            "topic_name": "Example",
                            "root": str(root),
                        }
                    ],
                }
            )
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def enqueue(
        self,
        agent_id: str,
        message_id: int,
        *,
        provider_session_id: str | None = None,
        thread_id: int | None = None,
    ) -> str:
        state = HubState.open(self.config.state_path, codex_permission_profile=None)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=70 + message_id if thread_id is None else thread_id,
                title="Example",
            )
            agent = self.config.require_agent(agent_id)
            session = state.activate_agent(
                topic.topic_id, agent_id, agent.default_model, agent.default_effort
            )
            if provider_session_id is not None:
                session = state.bind_provider_session(session.session_id, provider_session_id, None)
            job, _ = state.enqueue_provider_job(
                idempotency_key=f"telegram:-1001234567890:{message_id}",
                chat_id=-1001234567890,
                message_id=message_id,
                topic_id=topic.topic_id,
                agent_id=agent_id,
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=session.provider_session_id,
                model=session.model,
                effort=session.effort,
                payload_text="durable task",
                context_watermark=None,
                handoff_id=None,
            )
        finally:
            state.close()
        return job.job_id

    def bind_dynamic_project(self, project_id: str, root: Path, chat_id: int) -> None:
        document = json.loads(self.config.registry_path.read_text())
        document["projects"].append(
            {
                "project_id": project_id,
                "display_name": "Dynamic",
                "topic_name": "Dynamic",
                "root": str(root),
            }
        )
        self.config.registry_path.write_text(json.dumps(document))
        state = HubState.open(self.config.state_path, codex_permission_profile=None)
        try:
            with state._immediate_transaction():
                state._connection.execute(
                    """INSERT INTO project_onboarding_workflows
                       (workflow_id,owner_user_id,display_name,project_id,base_root,
                        canonical_root,stage,telegram_chat_id,telegram_access_hash,
                        expires_at,created_at,updated_at)
                       VALUES ('dynamic-workflow',42,'Dynamic',?,?,?,'completed',?,123,
                               '2099-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',
                               '2026-01-01T00:00:00+00:00')""",
                    (project_id, str(root.parent), str(root), chat_id),
                )
                state._connection.execute(
                    """INSERT INTO project_group_bindings
                       (project_id,telegram_chat_id,canonical_root,workflow_id,created_at)
                       VALUES (?,?,?,'dynamic-workflow','2026-01-01T00:00:00+00:00')""",
                    (project_id, chat_id, str(root)),
                )
        finally:
            state.close()

    def test_running_worker_resolves_relocated_root_before_new_execution(self) -> None:
        state = HubState.open(self.config.state_path, codex_permission_profile=None)
        try:
            edit = ProjectEditStore(state, self.config.registry_path)
            workflow = edit.start(owner_user_id=42, project_ids=("example-project",))
            workflow = edit.select_project(
                42, edit.project_options(workflow.workflow_id)[0].option_id
            )
            edit.choose_relocation(42, workflow.workflow_id)
            target = self.config.registry_path.parent / "example-project"
            option = next(
                item for item in edit.root_options(workflow.workflow_id) if item.root == target
            )
            edit.select_root(42, option.option_id)
            edit.confirm(42, workflow.workflow_id)
            edit.apply(workflow.workflow_id)
        finally:
            state.close()

        adapter = Adapter("opencode")
        worker = ExternalQueueWorker(
            self.config, "opencode", registry=self.registry, adapter=cast(Any, adapter)
        )
        self.addCleanup(worker.close)
        self.enqueue("opencode", 61)
        self.assertTrue(worker.run_cycle())
        self.assertEqual(adapter.last_cwd, target)

    def test_three_claude_slots_have_separate_state_and_adapter_instances(self) -> None:
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            False,
            False,
            "sonnet",
            "high",
            executable="claude",
        )
        config = replace(
            self.config,
            agents=(*self.config.agents, claude),
            external_worker_agent_ids=(*self.config.external_worker_agent_ids, "claude"),
            claude_worker_count=3,
        )
        workers = [
            ExternalQueueWorker(config, "claude", registry=self.registry, worker_slot=slot)
            for slot in (1, 2, 3)
        ]
        try:
            self.assertEqual(
                [worker.worker_id for worker in workers],
                ["claude-worker", "claude-worker-2", "claude-worker-3"],
            )
            self.assertEqual(len({id(worker.state) for worker in workers}), 3)
            self.assertEqual(len({id(worker.adapter) for worker in workers}), 3)
            with self.assertRaisesRegex(RuntimeError, "worker slot"):
                ExternalQueueWorker(config, "claude", registry=self.registry, worker_slot=4)
        finally:
            for worker in workers:
                worker.close()

    def test_cli_starts_only_configured_claude_slot(self) -> None:
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            False,
            False,
            "sonnet",
            "high",
            executable="claude",
        )
        config = replace(
            self.config,
            agents=(*self.config.agents, claude),
            external_worker_agent_ids=(*self.config.external_worker_agent_ids, "claude"),
            claude_worker_count=3,
        )
        started: list[int] = []

        class FakeWorker:
            def __init__(self, _config: HubConfig, _agent_id: str, *, worker_slot: int = 1) -> None:
                started.append(worker_slot)

            def run_forever(self, *, poll_seconds: float) -> None:
                pass

            def close(self) -> None:
                pass

        with (
            patch("hermes_codex_router.cli.load_external_worker_config", return_value=config),
            patch("hermes_codex_router.cli.ExternalQueueWorker", FakeWorker),
        ):
            self.assertEqual(
                main(["worker", "example.json", "--agent", "claude", "--slot", "3"]), 0
            )
            self.assertEqual(
                main(["worker", "example.json", "--agent", "claude", "--slot", "4"]), 2
            )
        self.assertEqual(started, [3])

    def test_claude_worker_commits_visible_result_to_existing_outbox(self) -> None:
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            False,
            False,
            "sonnet",
            "high",
            executable="claude",
        )
        config = replace(
            self.config,
            agents=(*self.config.agents, claude),
            external_worker_agent_ids=(*self.config.external_worker_agent_ids, "claude"),
        )
        state = HubState.open(config.state_path, codex_permission_profile=None)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=145,
                title="Example",
            )
            session = state.activate_agent(topic.topic_id, "claude", "sonnet", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="claude:fictional:first",
                chat_id=topic.chat_id,
                message_id=145,
                topic_id=topic.topic_id,
                agent_id="claude",
                session_id=session.session_id,
                session_generation=session.generation,
                model="sonnet",
                effort="high",
                payload_text="fictional task",
            )
        finally:
            state.close()
        adapter = Adapter("claude")
        worker = ExternalQueueWorker(
            config, "claude", registry=self.registry, adapter=cast(Any, adapter)
        )
        try:
            self.assertTrue(worker.run_cycle())
            completed = worker.state.get_provider_job(job.job_id)
            self.assertEqual(completed.status, "result_ready")
            self.assertEqual(
                worker.state.get_session(session.session_id).provider_session_id,
                adapter.last_session_id,
            )
            self.assertEqual(adapter.calls, 1)
        finally:
            worker.close()

    def test_running_worker_loads_new_dynamic_project_before_provider_boundary(self) -> None:
        adapter = Adapter("opencode")
        worker = ExternalQueueWorker(
            self.config, "opencode", registry=self.registry, adapter=cast(Any, adapter)
        )
        dynamic_root = self.config.registry_path.parent / "dynamic"
        dynamic_root.mkdir()
        subprocess.run(("git", "init", "-q", str(dynamic_root)), check=True)
        self.bind_dynamic_project("dynamic", dynamic_root, -1002222222222)
        state = HubState.open(self.config.state_path, codex_permission_profile=None)
        try:
            topic = state.observe_topic(
                project_id="dynamic", chat_id=-1002222222222, thread_id=7, title="Dynamic"
            )
            session = state.activate_agent(topic.topic_id, "opencode", "model-1", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="dynamic:first",
                chat_id=topic.chat_id,
                message_id=1,
                topic_id=topic.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="first dynamic turn",
            )
        finally:
            state.close()
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(worker.state.get_provider_job(job.job_id).status, "result_ready")
        finally:
            worker.close()

    def test_owner_direct_message_job_reaches_provider_with_exact_project(self) -> None:
        config = replace(self.config, direct_message_project_id="example-project")
        state = HubState.open(config.state_path, codex_permission_profile=None)
        try:
            topic = state.observe_topic(
                project_id="example-project", chat_id=42, thread_id=1, title="Direct"
            )
            session = state.activate_agent(topic.topic_id, "opencode", "model-1", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="direct:42:1",
                chat_id=42,
                message_id=1,
                topic_id=topic.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="direct owner turn",
            )
        finally:
            state.close()
        adapter = Adapter("opencode")
        worker = ExternalQueueWorker(config, "opencode", adapter=cast(Any, adapter))
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(worker.state.get_provider_job(job.job_id).status, "result_ready")
        finally:
            worker.close()

    def test_dynamic_root_drift_fails_while_leased_without_provider_call(self) -> None:
        dynamic_root = self.config.registry_path.parent / "dynamic"
        replacement = self.config.registry_path.parent / "replacement"
        for root in (dynamic_root, replacement):
            root.mkdir()
            subprocess.run(("git", "init", "-q", str(root)), check=True)
        self.bind_dynamic_project("dynamic", dynamic_root, -1002222222222)
        adapter = Adapter("opencode")
        worker = ExternalQueueWorker(self.config, "opencode", adapter=cast(Any, adapter))
        state = HubState.open(self.config.state_path, codex_permission_profile=None)
        try:
            topic = state.observe_topic(
                project_id="dynamic", chat_id=-1002222222222, thread_id=7, title="Dynamic"
            )
            session = state.activate_agent(topic.topic_id, "opencode", "model-1", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="dynamic:drift",
                chat_id=topic.chat_id,
                message_id=2,
                topic_id=topic.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="must not run",
            )
        finally:
            state.close()
        document = json.loads(self.config.registry_path.read_text())
        document["projects"][-1]["root"] = str(replacement)
        self.config.registry_path.write_text(json.dumps(document))
        try:
            self.assertTrue(worker.run_cycle())
            failed = worker.state.get_provider_job(job.job_id)
            self.assertEqual((failed.status, failed.error_class), ("failed", "pre_execution"))
            self.assertEqual(adapter.calls, 0)
        finally:
            worker.close()

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

    def test_existing_session_receives_full_contract_once_after_contract_rollout(self) -> None:
        job_id = self.enqueue("opencode", 40, provider_session_id="existing-session")
        adapter = Adapter("opencode")
        worker = self.worker("opencode", adapter)
        try:
            self.assertTrue(worker.run_cycle())
            self.assertIn("TELEGRAM INTERACTION CONTRACT v1", adapter.last_prompt)
            job = worker.state.get_provider_job(job_id)
            self.assertEqual(worker.state.telegram_contract_version(job.session_id), 1)

            outbox = worker.state.lease_telegram_outbox("opencode", "sender")
            assert outbox is not None and outbox.lease_token is not None
            complete_final_delivery(
                worker.state, outbox.outbox_id, outbox.lease_token, telegram_message_id=400
            )
            session = worker.state.get_session(job.session_id)
            second, _ = worker.state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:41",
                chat_id=-1001234567890,
                message_id=41,
                topic_id=job.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=session.provider_session_id,
                model=session.model,
                effort=session.effort,
                payload_text="next durable task",
            )
            self.assertTrue(worker.run_cycle())
            self.assertIn("TELEGRAM TRANSPORT REMINDER v1", adapter.last_prompt)
            self.assertNotIn("TELEGRAM INTERACTION CONTRACT v1", adapter.last_prompt)
            self.assertEqual(worker.state.get_provider_job(second.job_id).status, "result_ready")
        finally:
            worker.close()

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
            complete_final_delivery(
                worker.state,
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

    def test_emergency_stop_after_lease_cancels_before_the_provider_runs(self) -> None:
        job_id = self.enqueue("opencode", 31)
        adapter = Adapter("opencode")
        worker = self.worker("opencode", adapter)
        original_lease = worker.state.lease_provider_job
        topics: list[int] = []

        def lease_then_emergency_stop(*args: object, **kwargs: object) -> object:
            leased = cast(Any, original_lease)(*args, **kwargs)
            if leased is not None:
                topics.append(leased.topic_id)
                _, _, pending = worker.state.request_emergency_stop(
                    topic_id=leased.topic_id,
                    chat_id=-1001234567890,
                    message_id=931,
                    target_agent_id="codex",
                )
                self.assertTrue(pending)
            return leased

        worker.state.lease_provider_job = lease_then_emergency_stop  # type: ignore[method-assign]
        try:
            worker.run_cycle()
            self.assertEqual(adapter.calls, 0)
            job = worker.state.get_provider_job(job_id)
            self.assertEqual((job.status, job.error_code), ("cancelled", "emergency_stop"))
            self.assertIsNone(pending_stop(worker.state, topics[0], "opencode"))
        finally:
            worker.close()

    def test_a_stop_after_the_final_check_still_wins_at_the_result_commit(self) -> None:
        job_id = self.enqueue("opencode", 34)
        adapter = Adapter("opencode")
        worker = self.worker("opencode", adapter)
        prepare_artifacts = external_worker_module.prepare_worker_artifacts
        requests: list[str] = []

        def stop_while_preparing_artifacts(*args: Any, **kwargs: Any) -> Any:
            # R-021: the stop lands after the worker's last stop check.
            job = worker.state.get_provider_job(job_id)
            request_id, _, pending = worker.state.request_emergency_stop(
                topic_id=job.topic_id,
                chat_id=-1001234567890,
                message_id=934,
                target_agent_id="opencode",
            )
            requests.append(request_id)
            self.assertTrue(pending)
            self.assertTrue(
                worker.state.enqueue_emergency_stop_notice(
                    request_id, "Останавливаю активную работу."
                )
            )
            return prepare_artifacts(*args, **kwargs)

        try:
            with (
                patch.object(
                    external_worker_module,
                    "prepare_worker_artifacts",
                    stop_while_preparing_artifacts,
                ),
                self.assertNoLogs("hermes_codex_router", level="WARNING"),
            ):
                self.assertTrue(worker.run_cycle())
            self.assertEqual(adapter.calls, 1)
            job = worker.state.get_provider_job(job_id)
            self.assertEqual((job.status, job.error_code), ("cancelled", "emergency_stop"))
            notice = worker.state._connection.execute(
                "SELECT kind,status FROM task_lifecycle_notices WHERE stop_request_id=?",
                (requests[0],),
            ).fetchone()
            self.assertEqual(tuple(notice), ("stop_requested", "pending"))
            self.assertIsNone(
                worker.state._connection.execute(
                    "SELECT outbox_id FROM telegram_outbox WHERE job_id=?", (job_id,)
                ).fetchone()
            )
            self.assertIsNone(pending_stop(worker.state, job.topic_id, "opencode"))
            codes = [
                str(row[0])
                for row in worker.state._connection.execute(
                    "SELECT code FROM runtime_events WHERE component = 'opencode'"
                )
            ]
            self.assertIn("provider_turn_stopped", codes)
            self.assertNotIn("queued_provider_error", codes)
        finally:
            worker.close()

    def test_stop_during_unconfirmed_external_failure_retains_root_exclusion(self) -> None:
        job_id = self.enqueue("opencode", 35)
        peer_id = self.enqueue("antigravity", 36)
        worker = self.worker("opencode", Adapter("opencode"))

        def stop_then_fail(*_args: Any, **_kwargs: Any) -> Any:
            job = worker.state.get_provider_job(job_id)
            _, _, pending = worker.state.request_emergency_stop(
                topic_id=job.topic_id,
                chat_id=-1001234567890,
                message_id=935,
                target_agent_id="opencode",
            )
            self.assertTrue(pending)
            raise OSError("fictional artifact failure")

        try:
            with patch.object(external_worker_module, "prepare_worker_artifacts", stop_then_fail):
                self.assertTrue(worker.run_cycle())
            job = worker.state.get_provider_job(job_id)
            self.assertEqual((job.status, job.error_code), ("indeterminate", "OSError"))
            self.assertIsNotNone(pending_stop(worker.state, job.topic_id, "opencode"))
            codes = [
                str(row[0])
                for row in worker.state._connection.execute(
                    "SELECT code FROM runtime_events WHERE component = 'opencode'"
                )
            ]
            self.assertNotIn("provider_turn_stopped", codes)
            self.assertIn("queued_provider_error", codes)
            self.assertEqual(worker._provider_state, "unavailable")
            self.assertIsNotNone(
                persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
            )
            self.assertIsNone(worker.state.lease_provider_job("antigravity", "fictional-peer"))
            self.assertEqual(worker.state.get_provider_job(peer_id).status, "queued")
        finally:
            worker.close()

    def test_external_interrupt_exception_retains_native_outcome_uncertainty(self) -> None:
        job_id = self.enqueue("opencode", 37)
        peer_id = self.enqueue("antigravity", 38)
        entered = threading.Event()
        interrupted = threading.Event()

        class InterruptedAdapter(Adapter):
            def run_turn(self, **kwargs: object) -> ExternalTurnResult:
                self.calls += 1
                entered.set()
                if not interrupted.wait(3):
                    raise AssertionError("fictional interrupt was not requested")
                raise ExternalTurnInterrupted("owned process ended; remote native turn unconfirmed")

            def interrupt(self) -> bool:
                interrupted.set()
                return True

        adapter = InterruptedAdapter("opencode")
        worker = self.worker("opencode", adapter)

        def request_stop() -> None:
            if not entered.wait(2):
                return
            state = HubState.open(self.config.state_path, codex_permission_profile=None)
            try:
                job = state.get_provider_job(job_id)
                state.request_emergency_stop(
                    topic_id=job.topic_id,
                    chat_id=job.chat_id,
                    message_id=937,
                    target_agent_id="opencode",
                )
            finally:
                state.close()

        sender = threading.Thread(target=request_stop)
        try:
            sender.start()
            self.assertTrue(worker.run_cycle())
            job = worker.state.get_provider_job(job_id)
            self.assertEqual(
                (job.status, job.error_class), ("indeterminate", "ambiguous_execution")
            )
            self.assertIsNotNone(pending_stop(worker.state, job.topic_id, "opencode"))
            self.assertIsNotNone(
                persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
            )
            self.assertIsNone(worker.state.lease_provider_job("antigravity", "fictional-peer"))
            self.assertEqual(worker.state.get_provider_job(peer_id).status, "queued")
            self.assertIsNone(
                worker.state._connection.execute(
                    "SELECT job_id FROM provider_turn_terminal_evidence WHERE job_id=?", (job_id,)
                ).fetchone()
            )
            self.assertEqual(adapter.calls, 1)
        finally:
            interrupted.set()
            sender.join(2)
            worker.close()

    def test_an_unfinished_older_stop_never_interrupts_or_cancels_later_work(self) -> None:
        old_id = self.enqueue("opencode", 32, thread_id=132)
        state = HubState.open(self.config.state_path, codex_permission_profile=None)
        try:
            leased = state.lease_provider_job("opencode", "crashed-worker")
            assert leased is not None and leased.lease_token is not None
            self.assertEqual(leased.job_id, old_id)
            state.mark_provider_job_executing(leased.job_id, leased.lease_token)
            _, _, pending = state.request_emergency_stop(
                topic_id=leased.topic_id,
                chat_id=-1001234567890,
                message_id=932,
                target_agent_id="opencode",
            )
            self.assertTrue(pending)
            # The worker died after cancelling, before completing the stop.
            state.cancel_active_provider_job(leased.job_id, leased.lease_token)
            topic_id = leased.topic_id
        finally:
            state.close()
        later_id = self.enqueue("opencode", 33, thread_id=132)
        adapter = InterruptibleAdapter("opencode")
        worker = self.worker("opencode", adapter)
        try:
            worker.run_cycle()
            self.assertEqual(adapter.calls, 1)
            self.assertFalse(adapter.interrupted.is_set())
            self.assertEqual(worker.state.get_provider_job(later_id).status, "result_ready")
            self.assertIsNotNone(pending_stop(worker.state, topic_id, "opencode"))
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
            notice = worker.state.get_telegram_outbox_for_job(job_id).telegram_html
            self.assertIn("did not retry", notice)
            self.assertIn("What happened:", notice)
            self.assertIn("Saved:", notice)
            self.assertIn("Next:", notice)
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
        state = HubState.open(config.state_path, codex_permission_profile=None)
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
        verification = HubState.open(config.state_path, codex_permission_profile=None)
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
        controller.state = HubState.open(config.state_path, codex_permission_profile=None)
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
        controller.state = HubState.open(config.state_path, codex_permission_profile=None)
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
        controller.state = HubState.open(config.state_path, codex_permission_profile=None)
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

    def test_isolated_controller_refresh_preserves_models_without_rpc(self) -> None:
        from hermes_codex_router.provider_catalog import ProviderModel

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = replace(
            self.config,
            codex_model_provider="example-route",
            agents=self.config.agents
            + (
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "configured",
                    "high",
                ),
            ),
        )
        cache = controller._catalog_cache()
        cache.store(
            "codex",
            (
                ProviderModel("gpt-5.6-sol", "A", ("low", "high")),
                ProviderModel("claude-sonnet-4-6", "B", ("medium",)),
                ProviderModel("gemini-3-flash", "C", ("high",)),
            ),
            source_version="codex model/list",
        )
        with (
            patch.object(controller, "_uses_external_codex_worker", return_value=True),
            patch.object(
                controller,
                "_discover_provider_models",
                side_effect=AssertionError("provider invoked"),
            ),
        ):
            refreshed = controller._provider_catalog("codex", refresh=True)
            warm = controller._provider_catalog("codex")
        self.assertEqual([m.model_id for m in refreshed.models], ["gpt-5.6-sol"])
        self.assertEqual(warm.models, refreshed.models)
        self.assertEqual(warm.updated_at, refreshed.updated_at)
        self.assertTrue(cache.is_stale("codex"))

    def test_exact_route_cache_keeps_tagged_models_and_rejects_old_union_cache(self) -> None:
        from hermes_codex_router.provider_catalog import ProviderModel

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = replace(
            self.config,
            codex_model_provider="example-route",
            agents=self.config.agents
            + (
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-6-astra",
                    "high",
                ),
            ),
        )
        cache = controller._catalog_cache()
        models = (
            ProviderModel("gpt-6-astra", "GPT", ("high",)),
            ProviderModel("special-model", "Route", ("low",)),
        )
        with patch.object(controller, "_uses_external_codex_worker", return_value=True):
            cache.store(
                "codex", models, source_version="codex model/list route-filtered example-route"
            )
            self.assertEqual(
                [item.model_id for item in controller._provider_catalog("codex").models],
                ["gpt-6-astra", "special-model"],
            )
            self.assertEqual(
                [item.model_id for item in controller._cached_provider_catalog("codex").models],
                ["gpt-6-astra", "special-model"],
            )
            cache.store("codex", models, source_version="codex model/list example-route")
            self.assertEqual(
                [item.model_id for item in controller._cached_provider_catalog("codex").models],
                ["gpt-6-astra"],
            )

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
        state = HubState.open(config.state_path, codex_permission_profile=None)
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
        self.assertEqual(FakeWorker.instance.poll_seconds, 0.2)
        self.assertTrue(FakeWorker.instance.closed)
        self.assertEqual(main(["worker", str(config_path), "--agent", "missing"]), 2)

    def test_worker_collects_and_commits_staged_artifacts(self) -> None:
        job_id = self.enqueue("antigravity", 55)
        adapter = Adapter("antigravity")
        adapter.generate_artifact = True
        worker = self.worker("antigravity", adapter)
        try:
            self.assertTrue(worker.run_cycle())
            job = worker.state.get_provider_job(job_id)
            self.assertEqual(job.status, "result_ready")
            outbox = worker.state.get_telegram_outbox_for_job(job_id)
            parts = worker.state.get_telegram_outbox_parts(outbox.outbox_id)
            self.assertEqual(len(parts), 2)
            self.assertEqual(parts[0].part_type, "text")
            self.assertEqual(parts[1].part_type, "document")
            self.assertEqual(parts[1].file_name, "diagram.png")
            self.assertIsNotNone(parts[1].file_path)
            assert parts[1].file_path is not None
            self.assertTrue(Path(parts[1].file_path).is_file())
            self.assertIsNotNone(parts[1].file_size)
            self.assertIsNotNone(parts[1].file_sha256)
        finally:
            worker.close()
