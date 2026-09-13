"""Offline journey: native fictional store, real ingress/SQLite/worker/outbox."""

from __future__ import annotations

import unittest
from typing import Any, cast

import test_codex_session_adoption as adoption_fixtures
import test_codex_worker as worker_fixtures
import test_service_integration as ingress_fixtures
from test_outbox_sender import Bot

from hermes_codex_router.codex_appserver import CodexThread, CodexThreadMetadata, TurnResult
from hermes_codex_router.codex_session_adoption import attach_codex_session
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.registry import load_registry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.session_adoption_state import CodexSessionOrigins
from hermes_codex_router.state import HubState


class SessionAdoptionJourneyTests(unittest.TestCase):
    def test_attach_replace_delivery_restart_and_local_interval(self) -> None:
        for replacement in (False, True):
            for mode in ("stdio-fallback", "socket"):
                with self.subTest(replacement=replacement, transport=mode):
                    fixture = adoption_fixtures.CodexSessionAdoptionTests()
                    fixture.setUp()
                    try:
                        self.journey(fixture, replacement, mode)
                    finally:
                        fixture.doCleanups()

    def journey(self, fixture, replacement, mode):
        config = fixture.config
        registry = load_registry(config.registry_path)
        state = HubState.open(config.state_path)
        try:
            topic = state.find_topic(-1001234567890, 7)
            assert topic is not None
            previous = None
            if replacement:
                previous = state.activate_agent(topic.topic_id, "codex", "gpt-5.6-sol", "high")
                state.bind_provider_session(previous.session_id, "previous-thread", None)
                state.record_forwarded_quote(
                    topic_id=topic.topic_id,
                    chat_id=topic.chat_id,
                    message_id=1,
                    observer_agent_id="hub",
                    text="Old topic quote",
                )
            # This store belongs to the fictional provider, not Hub's journal.
            native_history = ["CLI marker before attach"]
            calls = []
            root = fixture.root

            class Client(worker_fixtures.WorkerClient):
                def read_thread_metadata(self, **kwargs):
                    calls.append(("read", kwargs["thread_id"]))
                    return CodexThreadMetadata("example-thread", root, "openai", "notLoaded")

                def start_thread(self, **kwargs):
                    raise AssertionError("adopted journey must never start a thread")

                def resume_thread(self, **kwargs):
                    calls.append(("resume", kwargs["thread_id"]))
                    return CodexThread("example-thread", root, "gpt-5.6-sol", "openai")

                def start_turn(self, **kwargs):
                    calls.append(("turn", kwargs["thread_id"]))
                    assert "Old topic quote" not in kwargs["text"]
                    return super().start_turn(**kwargs)

                def wait_for_turn(self, turn_id):
                    return TurnResult(" | ".join(native_history), None, None)

            result = attach_codex_session(
                config,
                project_id="example-project",
                chat_id=topic.chat_id,
                thread_id=7,
                codex_thread_id="example-thread",
                apply=True,
                confirm_cli_closed=True,
                replace_session=previous.session_id if previous else None,
                inspector=fixture.inspector,
            )
            session_id = str(result["hub_session_id"])
            self.assertEqual(state.get_session(session_id).writer_mode, "local")
            if previous:
                self.assertEqual(state.get_session(previous.session_id).status, "archived")
                self.assertEqual(
                    state.get_session(previous.session_id).provider_session_id, "previous-thread"
                )

            service = cast(Any, ProjectHubService.__new__(ProjectHubService))
            service.config, service.registry, service.state = config, registry, state
            service.agent = config.agents[0]
            service.telegram = ingress_fixtures.FakeTelegram()
            service.usernames = {"codex": service.agent.telegram_username}
            service._codex_client = None

            def send(mid, text):
                return service.handle_update(ingress_fixtures.update(mid, text, thread_id=7))

            self.assertTrue(send(10, "/return"))
            self.assertEqual(calls, [])
            self.assertTrue(send(11, "Continue"))

            def deliver():
                supervisor = worker_fixtures.WorkerSupervisor(Client())
                cast(Any, supervisor).transport_mode = mode
                worker = CodexQueueWorker(
                    config, registry=registry, supervisor=cast(Any, supervisor)
                )
                try:
                    self.assertTrue(worker.run_cycle())
                finally:
                    worker.close()
                bot = Bot()
                sender = TelegramOutboxSender(config, telegram_bots={"codex": bot})
                try:
                    self.assertTrue(sender.run_cycle())
                finally:
                    sender.close()
                return bot

            first = deliver()
            self.assertTrue(any("CLI marker before attach" in html for _, _, html in first.sent))
            self.assertTrue(send(20, "/local"))
            native_history.append("CLI marker during local interval")
            self.assertTrue(send(30, "/return"))
            self.assertEqual(len([name for name, _ in calls if name == "turn"]), 1)
            # Reopen Hub state and construct a fresh worker/client: no in-memory binding.
            state.close()
            state = HubState.open(config.state_path)
            service.state = state
            self.assertTrue(send(31, "Continue after local work"))
            second = deliver()
            self.assertTrue(
                any("CLI marker during local interval" in html for _, _, html in second.sent)
            )
            self.assertTrue(all(thread == "example-thread" for _, thread in calls))
            self.assertEqual(len([name for name, _ in calls if name == "turn"]), 2)
            self.assertEqual(
                CodexSessionOrigins(state).require(session_id).activation_message_id, 10
            )
            self.assertTrue(
                all(
                    job.status == "completed"
                    for job in state.provider_jobs_for_topic(topic.topic_id)
                )
            )
        finally:
            state.close()
