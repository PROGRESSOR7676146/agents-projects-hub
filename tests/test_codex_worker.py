from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import CodexThread, RateLimits, TurnResult
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.external_runtime import ExternalTurnResult
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState


class WorkerClient:
    def __init__(self, *, fail_after_start: bool = False) -> None:
        self.fail_after_start = fail_after_start
        self.turns = 0

    def start_thread(self, **kwargs: object) -> CodexThread:
        return CodexThread("thread-1", Path(str(kwargs["cwd"])), "gpt-5.6-sol", "openai")

    def resume_thread(self, **kwargs: object) -> CodexThread:
        return self.start_thread(**kwargs)

    def start_turn(self, **_kwargs: object) -> str:
        self.turns += 1
        if self.fail_after_start:
            raise RuntimeError("provider may have accepted the turn")
        return "turn-1"

    def wait_for_turn(self, _turn_id: str) -> TurnResult:
        return TurnResult("Visible answer", 1000, 100)

    def read_rate_limits(self) -> RateLimits:
        return RateLimits(None, None)

    def close(self) -> None:
        pass


class WorkerSupervisor:
    def __init__(self, client: WorkerClient) -> None:
        self.client_value = client
        self.stopped = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def client(self) -> WorkerClient:
        return self.client_value

    def stop(self) -> None:
        self.stopped = True


class CodexQueueWorkerTests(unittest.TestCase):
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
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-5.6-sol",
                    "high",
                ),
            ),
            dispatch_mode="queue",
            queue_runtime="external",
        )
        self.registry = ProjectRegistry(
            1, (base,), (Project("example-project", "Example", "Example", root),)
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def enqueue(
        self,
        message_id: int = 1,
        payload: str = "durable task",
        provider_session_id: str | None = None,
    ) -> str:
        state = HubState.open(self.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=77,
                title="Example",
            )
            session = state.activate_agent(topic.topic_id, "codex", "gpt-5.6-sol", "high")
            if provider_session_id is not None:
                session = state.bind_provider_session(session.session_id, provider_session_id, None)
            job, _ = state.enqueue_provider_job(
                idempotency_key=f"telegram:-1001234567890:{message_id}",
                chat_id=-1001234567890,
                message_id=message_id,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=provider_session_id,
                model=session.model,
                effort=session.effort,
                payload_text=payload,
                context_watermark=None,
                handoff_id=None,
            )
            return job.job_id
        finally:
            state.close()

    def test_stdio_fallback_starts_a_new_thread_with_bounded_visible_context(self) -> None:
        state = HubState.open(self.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=77,
                title="Example",
            )
            state.record_visible_turn(
                topic.topic_id,
                agent_id="codex",
                provider="codex",
                model="gpt-5.6-sol",
                user_excerpt="Earlier question",
                response_excerpt="Earlier answer",
                provider_session_id="shared-thread",
            )
        finally:
            state.close()
        job_id = self.enqueue(1, "Current question", provider_session_id="shared-thread")
        started: list[str] = []
        prompts: list[str] = []

        class Client(WorkerClient):
            def start_thread(self, **kwargs: object) -> CodexThread:
                started.append("new")
                return super().start_thread(**kwargs)

            def resume_thread(self, **_kwargs: object) -> CodexThread:
                raise AssertionError("fallback must not resume a shared-socket thread")

            def start_turn(self, **kwargs: object) -> str:
                prompts.append(str(kwargs["text"]))
                return super().start_turn(**kwargs)

        class Supervisor(WorkerSupervisor):
            transport_mode = "stdio-fallback"

        worker = CodexQueueWorker(
            self.config,
            registry=self.registry,
            supervisor=cast(Any, Supervisor(Client())),
            worker_id="test-codex-worker",
        )
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
            self.assertEqual(started, ["new"])
            self.assertIn("Earlier question", prompts[0])
            self.assertIn("Earlier answer", prompts[0])
            self.assertTrue(prompts[0].endswith("CURRENT USER MESSAGE:\nCurrent question"))
        finally:
            worker.close()

    def test_running_codex_turn_absorbs_ready_followup_via_turn_steer(self) -> None:
        parent_id = self.enqueue(1, "first")
        entered = threading.Event()
        release = threading.Event()
        steered: list[str] = []

        class MainClient(WorkerClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                entered.set()
                self.release = release
                release.wait(3)
                return TurnResult("Combined answer", 1000, 100)

        class ControlClient:
            def steer_turn(self, **kwargs: object) -> str:
                steered.append(str(kwargs["text"]))
                release.set()
                return str(kwargs["turn_id"])

            def close(self) -> None:
                pass

        class Supervisor:
            transport_mode = "socket"

            def __init__(self) -> None:
                self.main = MainClient()
                self.calls = 0

            def start(self) -> None:
                pass

            def client(self) -> object:
                self.calls += 1
                return self.main if self.calls == 1 else ControlClient()

            def stop(self) -> None:
                pass

        supervisor = Supervisor()
        worker = CodexQueueWorker(
            self.config,
            registry=self.registry,
            supervisor=cast(Any, supervisor),
            worker_id="test-codex-worker",
        )
        child_ids: list[str] = []

        def send_followup() -> None:
            if entered.wait(1):
                child_ids.append(self.enqueue(2, "follow up now"))

        sender = threading.Thread(target=send_followup)
        try:
            sender.start()
            self.assertTrue(worker.run_cycle())
            sender.join(2)
            self.assertEqual(len(child_ids), 1)
            child_id = child_ids[0]
            self.assertEqual(steered, ["follow up now"])
            self.assertEqual(worker.state.get_provider_job(parent_id).status, "result_ready")
            self.assertEqual(worker.state.get_provider_job(child_id).status, "completed")
        finally:
            release.set()
            sender.join(1)
            worker.close()

    def test_emergency_stop_interrupts_running_codex_turn_without_model_analysis(self) -> None:
        job_id = self.enqueue()
        entered = threading.Event()
        release = threading.Event()
        interrupted = threading.Event()

        class MainClient(WorkerClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                entered.set()
                release.wait(3)
                raise RuntimeError("interrupted transport")

        class ControlClient:
            def interrupt_turn(self, **_kwargs: object) -> None:
                interrupted.set()
                release.set()

            def close(self) -> None:
                pass

        class Supervisor:
            transport_mode = "socket"

            def __init__(self) -> None:
                self.main = MainClient()
                self.calls = 0

            def start(self) -> None:
                pass

            def client(self) -> object:
                self.calls += 1
                return self.main if self.calls == 1 else ControlClient()

            def stop(self) -> None:
                pass

        worker = CodexQueueWorker(
            self.config,
            registry=self.registry,
            supervisor=cast(Any, Supervisor()),
            worker_id="test-codex-worker",
        )
        requested = threading.Event()

        def request_stop() -> None:
            if not entered.wait(1):
                return
            state = HubState.open(self.config.state_path)
            try:
                topic = state.find_topic(-1001234567890, 77)
                assert topic is not None
                state.request_emergency_stop(
                    topic_id=topic.topic_id,
                    chat_id=-1001234567890,
                    message_id=99,
                    target_agent_id="codex",
                )
                requested.set()
            finally:
                state.close()

        sender = threading.Thread(target=request_stop)
        try:
            sender.start()
            self.assertTrue(worker.run_cycle())
            self.assertTrue(requested.wait(1))
            topic = worker.state.find_topic(-1001234567890, 77)
            assert topic is not None
            self.assertTrue(interrupted.wait(2))
            self.assertEqual(worker.state.get_provider_job(job_id).status, "cancelled")
            self.assertIsNone(worker.state.pending_emergency_stop(topic.topic_id, "codex"))
        finally:
            release.set()
            sender.join(1)
            worker.close()

    def test_stdio_stop_wins_race_with_transport_eof(self) -> None:
        job_id = self.enqueue()
        entered = threading.Event()
        closed = threading.Event()
        clients: list[WorkerClient] = []

        class Client(WorkerClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                entered.set()
                closed.wait(3)
                raise EOFError("private stdio app-server closed")

            def close(self) -> None:
                closed.set()
                # Let wait_for_turn observe EOF before the monitor records that
                # close was caused by the durable stop request.
                threading.Event().wait(0.05)

        class Supervisor:
            transport_mode = "stdio-fallback"

            def start(self) -> None:
                pass

            def client(self) -> WorkerClient:
                client: WorkerClient = Client() if not clients else WorkerClient()
                clients.append(client)
                return client

            def stop(self) -> None:
                pass

        worker = CodexQueueWorker(
            self.config,
            registry=self.registry,
            supervisor=cast(Any, Supervisor()),
            worker_id="test-codex-worker",
        )

        def request_stop() -> None:
            if not entered.wait(1):
                return
            state = HubState.open(self.config.state_path)
            try:
                topic = state.find_topic(-1001234567890, 77)
                assert topic is not None
                state.request_emergency_stop(
                    topic_id=topic.topic_id,
                    chat_id=-1001234567890,
                    message_id=100,
                    target_agent_id="codex",
                )
            finally:
                state.close()

        sender = threading.Thread(target=request_stop)
        try:
            sender.start()
            self.assertTrue(worker.run_cycle())
            sender.join(1)
            topic = worker.state.find_topic(-1001234567890, 77)
            assert topic is not None
            self.assertEqual(worker.state.get_provider_job(job_id).status, "cancelled")
            self.assertIsNone(worker.state.pending_emergency_stop(topic.topic_id, "codex"))
            next_job_id = self.enqueue(message_id=101, payload="after stop")
            self.assertTrue(worker.run_cycle())
            self.assertEqual(
                worker.state.get_provider_job(next_job_id).status,
                "result_ready",
            )
            self.assertEqual(len(clients), 2)
        finally:
            closed.set()
            sender.join(1)
            worker.close()

    def worker(self, client: WorkerClient) -> CodexQueueWorker:
        return CodexQueueWorker(
            self.config,
            registry=self.registry,
            supervisor=cast(Any, WorkerSupervisor(client)),
            worker_id="test-codex-worker",
        )

    def test_worker_has_no_telegram_capability_and_uses_its_own_state_connection(self) -> None:
        controller_state = HubState.open(self.config.state_path)
        worker = self.worker(WorkerClient())
        try:
            self.assertFalse(hasattr(worker, "telegram"))
            self.assertIsNot(worker.state, controller_state)
        finally:
            worker.close()
            controller_state.close()

    def test_worker_starts_supervisor_and_is_stoppable(self) -> None:
        supervisor = WorkerSupervisor(WorkerClient())
        worker = CodexQueueWorker(
            self.config,
            registry=self.registry,
            supervisor=cast(Any, supervisor),
            worker_id="test-codex-worker",
        )
        try:
            worker.stop()
            worker.run_forever(poll_seconds=0.01)
            self.assertTrue(supervisor.started)
        finally:
            worker.close()

    def test_worker_restart_recovers_pre_execution_lease_and_commits_result_without_delivery(
        self,
    ) -> None:
        job_id = self.enqueue()
        first = self.worker(WorkerClient())
        try:
            leased = first.state.lease_provider_job("codex", "lost-worker", lease_seconds=1)
            assert leased is not None
            first.state.heartbeat_provider_job(
                job_id,
                leased.lease_token or "",
                lease_seconds=1,
                now=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
        finally:
            first.close()

        client = WorkerClient()
        resumed = self.worker(client)
        try:
            self.assertTrue(resumed.run_cycle())
            self.assertEqual(client.turns, 1)
            self.assertEqual(resumed.state.get_provider_job(job_id).status, "result_ready")
            self.assertEqual(resumed.state.get_telegram_outbox_for_job(job_id).status, "pending")
        finally:
            resumed.close()

    def test_provider_failure_after_execution_begins_is_indeterminate_and_never_retried(
        self,
    ) -> None:
        job_id = self.enqueue()
        worker = self.worker(WorkerClient(fail_after_start=True))
        try:
            self.assertTrue(worker.run_cycle())
            failed = worker.state.get_provider_job(job_id)
            self.assertEqual(failed.status, "indeterminate")
            self.assertEqual(failed.error_detail, "provider may have accepted the turn")
            self.assertFalse(worker.run_cycle())
        finally:
            worker.close()

    def test_controller_external_mode_leaves_delivery_to_sender(self) -> None:
        job_id = self.enqueue()
        worker = self.worker(WorkerClient())
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = self.config
        controller.external_services = {}
        self.assertFalse(controller.run_embedded_queue_cycle())
        controller.config = replace(self.config, outbox_runtime="external")
        self.assertFalse(controller.run_controller_outbox_cycle())
        state = HubState.open(self.config.state_path)
        try:
            self.assertEqual(state.get_provider_job(job_id).status, "result_ready")
            self.assertEqual(state.get_telegram_outbox_for_job(job_id).status, "pending")
        finally:
            state.close()

    def test_external_codex_mode_keeps_embedded_non_codex_compatibility(self) -> None:
        opencode = AgentDefinition(
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
        )
        config = replace(self.config, agents=self.config.agents + (opencode,))
        state = HubState.open(config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=77,
                title="Example",
            )
            session = state.activate_agent(topic.topic_id, "opencode", "provider-selected", "high")
            state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:open",
                chat_id=-1001234567890,
                message_id=2,
                topic_id=topic.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="external compatibility task",
                context_watermark=None,
                handoff_id=None,
            )
        finally:
            state.close()

        class Telegram:
            def send_html(self, *_args: object, **_kwargs: object) -> int:
                return 1

        class Adapter:
            def __init__(self) -> None:
                self.turns = 0

            def run_turn(self, **_kwargs: object) -> ExternalTurnResult:
                self.turns += 1
                return ExternalTurnResult("opencode", "Open answer", "open-1", "open-model")

        class External:
            def __init__(self) -> None:
                self.adapter = Adapter()
                self.telegram = Telegram()

        class ForbiddenSupervisor:
            def client(self) -> object:
                raise AssertionError("controller called Codex RPC")

        external = External()
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = self.registry
        controller.telegram = Telegram()
        controller.external_services = {"opencode": external}
        controller.supervisor = ForbiddenSupervisor()
        controller._codex_client = None
        self.assertTrue(controller.run_embedded_queue_cycle())
        self.assertEqual(external.adapter.turns, 1)

    def test_external_controller_does_not_start_or_own_codex_supervisor(self) -> None:
        class Telegram:
            def updates(self, **_kwargs: object) -> list[object]:
                raise KeyboardInterrupt

        class ForbiddenSupervisor:
            def start(self) -> None:
                raise AssertionError("controller started Codex supervisor")

            def client(self) -> object:
                raise AssertionError("controller called Codex RPC")

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = self.config
        controller.state = HubState.open(self.config.state_path)
        controller.agent = self.config.require_agent("codex")
        controller.telegram = Telegram()
        controller.supervisor = ForbiddenSupervisor()
        controller._codex_client = None
        controller.external_services = {}
        controller._start_controller_outbox_delivery = lambda: None  # type: ignore[method-assign]
        try:
            with self.assertRaises(KeyboardInterrupt):
                controller.run_forever()
        finally:
            controller.state.close()

    def test_external_controller_enqueues_codex_without_provider_rpc(self) -> None:
        class Telegram:
            def send_html(self, *_args: object, **_kwargs: object) -> int:
                return 1

        class ForbiddenSupervisor:
            def client(self) -> object:
                raise AssertionError("controller called Codex RPC")

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = self.config
        controller.registry = self.registry
        controller.state = HubState.open(self.config.state_path)
        controller.agent = self.config.require_agent("codex")
        controller.telegram = Telegram()
        controller.supervisor = ForbiddenSupervisor()
        controller._codex_client = None
        controller.usernames = {"codex": "example_codex_bot"}
        controller.external_services = {}
        try:
            update = {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "message_thread_id": 77,
                    "is_topic_message": True,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": -1001234567890, "type": "supergroup", "title": "Example"},
                    "text": "enqueue only",
                },
            }
            self.assertTrue(controller.handle_update(update))
            topic = controller.state.find_topic(-1001234567890, 77)
            assert topic is not None
            jobs = controller.state.provider_jobs_for_topic(topic.topic_id)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].status, "queued")
        finally:
            controller.state.close()

    def test_inline_rollback_cannot_overtake_existing_queued_work(self) -> None:
        self.enqueue()

        class Telegram:
            def __init__(self) -> None:
                self.sent: list[str] = []

            def send_html(self, _chat: int, _thread: int, text: str, **_kwargs: object) -> int:
                self.sent.append(text)
                return len(self.sent)

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = replace(self.config, dispatch_mode="inline", queue_runtime="embedded")
        controller.registry = self.registry
        controller.state = HubState.open(self.config.state_path)
        controller.agent = self.config.require_agent("codex")
        controller.telegram = Telegram()
        controller._codex_client = None
        controller.usernames = {"codex": "example_codex_bot"}
        controller.external_services = {}
        try:
            self.assertTrue(
                controller.handle_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 2,
                            "message_thread_id": 77,
                            "is_topic_message": True,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {
                                "id": -1001234567890,
                                "type": "supergroup",
                                "title": "Example",
                            },
                            "text": "must not overtake",
                        },
                    }
                )
            )
            self.assertTrue(
                any("queued work still exists" in item for item in controller.telegram.sent)
            )
        finally:
            controller.state.close()

    def test_external_controller_pool_status_never_runs_multi_auth(self) -> None:
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = replace(
            self.config,
            codex_multi_auth_dir=Path(self.tempdir.name),
            codex_multi_auth_executable=Path("/example/codex-multi-auth"),
        )
        with patch(
            "hermes_codex_router.service.read_codex_pool_status",
            side_effect=AssertionError("controller invoked multi-auth"),
        ) as reader:
            self.assertIsNone(controller._codex_pool())
            reader.assert_not_called()
