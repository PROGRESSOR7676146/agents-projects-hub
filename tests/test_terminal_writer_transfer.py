from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.registry import ExecutionRootError
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.topic_execution import resolve_topic_execution_root
from tests import test_embedded_queue_service as fixtures


class TerminalWriterTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.EmbeddedQueueServiceTests()
        self.fixture.setUp()
        self.client = fixtures.QueueClient()
        self.service, self.telegram = self.fixture.service(self.client)
        self.service.config = replace(self.service.config, dispatch_mode="inline")
        self.terminal = Mock()
        self.terminal.is_running.return_value = False
        self.service.terminal = cast(Any, self.terminal)
        self.project = self.service.registry.projects[0]
        self.topic = self.service.state.observe_topic(
            project_id=self.project.project_id,
            chat_id=-1001234567890,
            thread_id=77,
            title="Fictional terminal",
            execution_root=self.project.root,
        )
        self.session = self.service.state.activate_agent(
            self.topic.topic_id, "codex", "fictional", "high"
        )
        self.peer = HubState.open(self.service.config.state_path)

    def tearDown(self) -> None:
        self.peer.close()
        self.service.state.close()
        self.fixture.tearDown()

    def test_terminal_claim_precedes_provider_preparation_and_process_launch(self) -> None:
        def check_claim(**_kwargs: object) -> None:
            self.assertEqual(self.peer.get_session(self.session.session_id).writer_mode, "terminal")
            other_topic = self.peer.observe_topic(
                project_id=self.project.project_id,
                chat_id=self.topic.chat_id,
                thread_id=78,
                title="Fictional peer",
                execution_root=self.project.root,
            )
            other = self.peer.active_session(other_topic.topic_id)
            if other is None:
                other = self.peer.activate_agent(other_topic.topic_id, "codex", "fictional", "high")
            with self.assertRaises(StateError):
                self.peer.set_writer_mode(other.session_id, "local")

        original = self.client.start_thread

        def start_thread(**kwargs: object):
            check_claim()
            return original(**kwargs)

        self.terminal.start.side_effect = check_claim
        with patch.object(self.client, "start_thread", side_effect=start_thread):
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/terminal")))
        self.assertEqual(self.client.started_threads, 1)
        self.terminal.start.assert_called_once()
        self.assertEqual(self.terminal.start.call_args.kwargs["cwd"], self.project.root)

    def test_terminal_rejects_snapshot_change_before_any_provider_access(self) -> None:
        def change_session(*args: Any):
            root = resolve_topic_execution_root(*args)
            self.peer.bind_provider_session(
                self.session.session_id, "fictional-peer-thread", "fictional-peer-terminal"
            )
            return root

        with patch(
            "hermes_codex_router.service.resolve_topic_execution_root", side_effect=change_session
        ):
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/terminal")))
        self.assertEqual(self.client.started_threads, 0)
        self.terminal.start.assert_not_called()
        self.assertEqual(self.peer.get_session(self.session.session_id).writer_mode, "telegram")

    def test_ambiguous_launch_retains_claim_until_explicit_release(self) -> None:
        self.terminal.start.side_effect = RuntimeError("fictional ambiguous launch")
        try:
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/terminal")))
        except RuntimeError:
            self.fail("ambiguous launch escaped without a bounded ownership notice")
        self.assertEqual(self.peer.get_session(self.session.session_id).writer_mode, "terminal")
        self.assertTrue(self.service.handle_update(fixtures.update(2, "fictional task")))
        self.assertEqual(self.client.turn_threads, [])
        self.assertTrue(self.service.handle_update(fixtures.update(3, "/terminal")))
        self.terminal.start.assert_called_once()
        self.assertEqual(self.peer.get_session(self.session.session_id).writer_mode, "terminal")
        self.assertTrue(self.service.handle_update(fixtures.update(4, "/release")))
        self.terminal.release.assert_called_once()
        self.assertEqual(self.peer.get_session(self.session.session_id).writer_mode, "telegram")

    def test_unobserved_terminal_is_not_automatically_returned_to_telegram(self) -> None:
        self.service.state.bind_provider_session(
            self.session.session_id, "fictional-thread", "fictional-terminal"
        )
        self.service.state.set_writer_mode(self.session.session_id, "terminal")
        self.assertTrue(self.service.handle_update(fixtures.update(1, "fictional task")))
        self.assertEqual(self.client.turn_threads, [])
        self.assertEqual(self.peer.get_session(self.session.session_id).writer_mode, "terminal")

    def test_preparation_refusal_retains_claim_without_launch(self) -> None:
        with patch.object(
            self.service, "_ensure_provider_thread", side_effect=ExecutionRootError()
        ):
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/terminal")))
        self.terminal.start.assert_not_called()
        self.assertEqual(self.client.started_threads, 0)
        self.assertEqual(self.peer.get_session(self.session.session_id).writer_mode, "terminal")
