from __future__ import annotations

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
        session = state.active_session(topic.topic_id)
        self.assertEqual(session.provider_session_id, "example-cli-thread")
        self.assertEqual(session.writer_mode, "telegram")
        self.assertEqual(client.turns, 0)
        self.assertEqual(client.read_calls, 1)
        self.assertIsNotNone(CodexSessionOrigins(state).get(session.session_id))


if __name__ == "__main__":
    unittest.main()
