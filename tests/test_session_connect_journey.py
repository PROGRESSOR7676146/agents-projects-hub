from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import test_codex_worker as worker_fixtures
import test_service_integration as service_fixtures

from hermes_codex_router.codex_appserver import (
    CodexThreadMetadata,
    ConnectableCodexThread,
)
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.session_adoption_state import CodexSessionOrigins
from hermes_codex_router.session_connect import ConnectCandidate, SessionConnectStore
from hermes_codex_router.state import HubState


class HubBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str, object]] = []

    def send_html(self, chat_id, thread_id, text, *, reply_markup=None):
        self.sent.append((chat_id, thread_id, text, reply_markup))
        return 100 + len(self.sent)

    def send_chat_action(self, *_args, **_kwargs):
        return None

    def send_message_draft(self, *_args, **_kwargs):
        return None


class DirectControlBot(service_fixtures.FakeTelegram):
    def create_forum_topic(self, chat_id: int, name: str) -> int:
        self.created_topic = (chat_id, name)
        return 88


def direct_update(message_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": 42, "type": "private"},
            "text": text,
        },
    }


def direct_callback(message_id: int, callback_id: str, data: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": 42, "is_bot": False},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": 42, "type": "private"},
            },
        },
    }


class Client(worker_fixtures.WorkerClient):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.read_calls = 0

    def initialize(self):
        return None

    def list_connectable_threads(self, *, root):
        self.assert_root = root
        return (ConnectableCodexThread("example-cli-thread", "Сессия · saved", 10),)

    def read_thread_metadata(self, *, thread_id, cwd):
        self.read_calls += 1
        return CodexThreadMetadata(thread_id, cwd, "openai", "notLoaded")


class SessionConnectJourneyTests(unittest.TestCase):
    def test_discovery_progresses_while_productive_turn_waits(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job_id = fixture.enqueue()
        entered = threading.Event()
        release = threading.Event()

        class SlowWorker(ExternalQueueWorker):
            def _execute(self, job):
                assert job.lease_token is not None
                self.state.mark_provider_job_executing(job.job_id, job.lease_token)
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("fictional turn timed out")

        client = Client(fixture.registry.projects[0].root)
        workers: list[SlowWorker] = []

        def run_worker() -> None:
            worker = SlowWorker(
                fixture.config,
                "codex",
                registry=fixture.registry,
                supervisor=cast(Any, worker_fixtures.WorkerSupervisor(client)),
                worker_id="connect-worker",
            )
            workers.append(worker)
            try:
                worker.run_forever()
            finally:
                worker.close()

        runner = threading.Thread(target=run_worker, daemon=True)
        runner.start()
        self.assertTrue(entered.wait(2), "productive turn did not start")
        try:
            state = HubState.open(fixture.config.state_path)
            try:
                workflow = SessionConnectStore(state).start_topic(
                    owner_user_id=42,
                    project_id="example-project",
                    canonical_root=fixture.registry.projects[0].root,
                    chat_id=-1001234567890,
                    thread_id=77,
                    model="gpt-5.6-sol",
                    effort="high",
                )
                deadline = time.monotonic() + 1
                current = SessionConnectStore(state).get(workflow.workflow_id)
                while time.monotonic() < deadline:
                    current = SessionConnectStore(state).get(workflow.workflow_id)
                    if current.stage == "choosing_source":
                        break
                    time.sleep(0.02)
                self.assertEqual(current.stage, "choosing_source")
                self.assertEqual(state.get_provider_job(job_id).status, "executing")
                self.assertEqual(client.turns, 0)
                self.assertEqual(
                    state._connection.execute(
                        "SELECT COUNT(*) FROM session_connect_outbox WHERE workflow_id=?",
                        (workflow.workflow_id,),
                    ).fetchone()[0],
                    1,
                )
            finally:
                state.close()
        finally:
            release.set()
            if workers:
                workers[0].stop()
            runner.join(2)
            self.assertFalse(runner.is_alive())

    def test_topic_connect_uses_worker_marker_and_never_starts_a_turn(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = replace(
            fixture.config,
            hub_bot=HubTelegramBot("example_hub_bot", Path("/tmp/example-token")),
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        state = HubState.open(config.state_path)
        self.addCleanup(state.close)
        state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example",
        )
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = fixture.registry
        controller.state = state
        controller.agent = config.agents[0]
        controller.telegram = service_fixtures.FakeTelegram()
        controller.usernames = {"codex": controller.agent.telegram_username}
        controller._codex_client = None

        client = Client(fixture.registry.projects[0].root)
        worker = ExternalQueueWorker(
            config,
            "codex",
            registry=fixture.registry,
            supervisor=cast(Any, worker_fixtures.WorkerSupervisor(client)),
            worker_id="connect-worker",
        )
        self.addCleanup(worker.close)
        hub = HubBot()
        sender = TelegramOutboxSender(
            config,
            telegram_bots=cast(Any, {"codex": HubBot(), "hub": hub}),
            sender_id="connect-sender",
        )
        self.addCleanup(sender.close)

        self.assertTrue(controller.handle_update(service_fixtures.update(1, "/connect")))
        self.assertTrue(worker.run_cycle())
        self.assertTrue(sender.run_cycle())
        markup = hub.sent[-1][3]
        select = service_fixtures.callback_values(markup)[0]
        self.assertTrue(controller.handle_update(service_fixtures.callback(2, "select", select)))
        confirm = service_fixtures.callback_values(controller.telegram.markups[-1])[0]
        self.assertTrue(controller.handle_update(service_fixtures.callback(3, "confirm", confirm)))
        self.assertTrue(worker.run_cycle())
        self.assertTrue(sender.run_cycle())
        self.assertTrue(sender.run_cycle())

        topic = state.find_topic(-1001234567890, 77)
        assert topic is not None
        session = state.active_session(topic.topic_id)
        assert session is not None
        self.assertEqual(session.provider_session_id, "example-cli-thread")
        self.assertEqual(session.writer_mode, "telegram")
        self.assertEqual(client.turns, 0)
        self.assertEqual(client.read_calls, 1)
        self.assertIsNotNone(CodexSessionOrigins(state).get(session.session_id))

    def test_direct_connect_can_create_a_topic_without_provider_turn(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = replace(
            fixture.config,
            hub_bot=HubTelegramBot("example_hub_bot", Path("/tmp/example-token")),
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        state = HubState.open(config.state_path)
        self.addCleanup(state.close)
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = fixture.registry
        controller.state = state
        controller.agent = config.agents[0]
        controller.telegram = DirectControlBot()
        controller.usernames = {"codex": controller.agent.telegram_username}
        controller._codex_client = None
        controller.ingress_identity = "hub"
        controller.direct_messages_only = False

        client = Client(fixture.registry.projects[0].root)
        worker = ExternalQueueWorker(
            config,
            "codex",
            registry=fixture.registry,
            supervisor=cast(Any, worker_fixtures.WorkerSupervisor(client)),
            worker_id="connect-worker",
        )
        self.addCleanup(worker.close)
        hub = HubBot()
        sender = TelegramOutboxSender(
            config,
            telegram_bots=cast(Any, {"codex": HubBot(), "hub": hub}),
            sender_id="connect-sender",
        )
        self.addCleanup(sender.close)

        self.assertTrue(controller.handle_update(direct_update(10, "/connect")))
        project = service_fixtures.callback_values(controller.telegram.markups[-1])[0]
        self.assertTrue(controller.handle_update(direct_callback(11, "project", project)))
        self.assertTrue(worker.run_cycle())
        self.assertTrue(sender.run_cycle())
        source = service_fixtures.callback_values(hub.sent[-1][3])[0]
        self.assertTrue(controller.handle_update(direct_callback(12, "source", source)))
        new_topic = next(
            value
            for value in service_fixtures.callback_values(controller.telegram.markups[-1])
            if value.startswith("cx:n:")
        )
        self.assertTrue(controller.handle_update(direct_callback(13, "new", new_topic)))
        self.assertTrue(controller.handle_update(direct_update(14, "Saved work")))

        self.assertEqual(
            controller.telegram.created_topic,
            (-1001234567890, "Saved work"),
        )
        topic = state.find_topic(-1001234567890, 88)
        self.assertIsNotNone(topic)
        assert topic is not None
        self.assertEqual(client.turns, 0)
        self.assertEqual(state.provider_jobs_for_topic(topic.topic_id), ())

    def test_topic_accepts_local_helper_code_without_manual_identifiers(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = replace(
            fixture.config,
            hub_bot=HubTelegramBot("example_hub_bot", Path("/tmp/example-token")),
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        state = HubState.open(config.state_path)
        self.addCleanup(state.close)
        state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example",
        )
        issued = SessionConnectStore(state).issue_code(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=fixture.registry.projects[0].root,
            source=ConnectCandidate("", "example-cli-thread", "Сессия · saved", 10),
            model="gpt-5.6-sol",
            effort="high",
        )
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = fixture.registry
        controller.state = state
        controller.agent = config.agents[0]
        controller.telegram = service_fixtures.FakeTelegram()
        controller.usernames = {"codex": controller.agent.telegram_username}
        controller._codex_client = None

        self.assertTrue(
            controller.handle_update(service_fixtures.update(20, f"/connect {issued.code.lower()}"))
        )
        self.assertIn("Закройте CLI", controller.telegram.sent[-1][2])
        self.assertTrue(
            any(
                value.startswith("cx:ok:")
                for value in service_fixtures.callback_values(controller.telegram.markups[-1])
            )
        )
        self.assertEqual(
            state._connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
