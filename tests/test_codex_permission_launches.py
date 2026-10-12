from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from hermes_codex_router.local_transfer import LocalTransferError, local_resume_command
from hermes_codex_router.pilot import run_codex_pilot
from hermes_codex_router.registry import RegistryError
from hermes_codex_router.state import HubState
from hermes_codex_router.terminal import build_codex_remote_argv, build_codex_resume_argv
from hermes_codex_router.terminal_runtime import TerminalRuntime, TerminalRuntimeError
from tests import test_embedded_queue_service as fixtures

PROFILE = "example-project-policy"


class ManagedCodexLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.EmbeddedQueueServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = fixtures.QueueClient()
        self.service, self.telegram = self.fixture.service(self.client)
        self.service.state.close()
        self.service.config = replace(self.service.config, codex_permission_profile=PROFILE)
        self.service.state = HubState.open(
            self.service.config.state_path, codex_permission_profile=PROFILE
        )
        self.addCleanup(self.service.state.close)
        self.project = self.service.registry.projects[0]
        self.topic = self.service.state.observe_topic(
            project_id=self.project.project_id,
            chat_id=-1001234567890,
            thread_id=77,
            title="Example topic",
            execution_root=self.project.root,
        )
        self.session = self.service.state.activate_agent(
            self.topic.topic_id, "codex", "example-model", "high"
        )
        self.service.state.bind_provider_session(
            self.session.session_id, "example-thread", "example-terminal"
        )

    def test_local_refusal_precedes_observation_and_lease(self) -> None:
        with patch("hermes_codex_router.service.TurnObservation") as observer:
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/local")))
        observer.assert_not_called()
        self.assertEqual(
            self.service.state.get_session(self.session.session_id).writer_mode, "telegram"
        )
        self.assertEqual(self.client.turn_threads, [])

    def test_terminal_refusal_precedes_provider_preparation_and_lease(self) -> None:
        self.service.config = replace(self.service.config, dispatch_mode="inline")
        self.service.terminal = Mock()
        with patch.object(self.service, "_ensure_provider_thread") as preparation:
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/terminal")))
        preparation.assert_not_called()
        self.service.terminal.start.assert_not_called()
        self.assertEqual(
            self.service.state.get_session(self.session.session_id).writer_mode, "telegram"
        )

    def test_old_named_session_cannot_transfer_after_config_reverts_to_legacy(self) -> None:
        self.service.config = replace(self.service.config, codex_permission_profile=None)
        with patch("hermes_codex_router.service.TurnObservation") as observer:
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/local")))
        observer.assert_not_called()
        self.assertEqual(
            self.service.state.get_session(self.session.session_id).writer_mode, "telegram"
        )

    def test_local_builder_never_returns_an_unverified_managed_launch(self) -> None:
        with self.assertRaises(LocalTransferError):
            local_resume_command(
                "codex",
                None,
                "example-thread",
                self.project.root,
                permission_profile=PROFILE,
                codex_socket_path=self.service.config.codex_socket_path,
            )

    def test_terminal_adapters_refuse_managed_launch_before_process_inspection(self) -> None:
        with self.assertRaises(RegistryError):
            build_codex_resume_argv(
                thread_id="example-thread", cwd=self.project.root, permission_profile=PROFILE
            )
        with self.assertRaises(RegistryError):
            build_codex_remote_argv(
                thread_id="example-thread",
                cwd=self.project.root,
                permission_profile=PROFILE,
                socket_path=self.service.config.codex_socket_path,
            )
        runner = Mock()
        terminal = TerminalRuntime(
            socket_path=self.service.config.codex_socket_path,
            backend="tmux-only",
            permission_profile=PROFILE,
            run=runner,
        )
        with self.assertRaises(TerminalRuntimeError):
            terminal.start(
                name="example", title="Example", thread_id="example-thread", cwd=self.project.root
            )
        runner.assert_not_called()

    def test_pilot_refuses_before_state_provider_or_telegram_creation(self) -> None:
        with (
            patch("hermes_codex_router.pilot.HubState.open") as opened,
            patch("hermes_codex_router.pilot.CodexAppServerSupervisor") as supervisor,
            patch("hermes_codex_router.pilot.TelegramBotApi") as telegram,
        ):
            with self.assertRaisesRegex(ValueError, "managed Codex permission profiles"):
                run_codex_pilot(
                    self.service.config,
                    project_id=self.project.project_id,
                    chat_id=self.topic.chat_id,
                    thread_id=self.topic.thread_id,
                    topic_title="Example topic",
                )
        opened.assert_not_called()
        supervisor.assert_not_called()
        telegram.assert_not_called()

    def test_pilot_refuses_retained_managed_session_under_legacy_config(self) -> None:
        self.assert_pilot_refuses_retained_managed_session()

    def test_pilot_refuses_retained_managed_satellite_before_topic_mutation(self) -> None:
        other = self.service.state.activate_agent(self.topic.topic_id, "claude", "example", "high")
        self.assert_pilot_refuses_retained_managed_session()
        self.assertEqual(self.service.state.get_session(other.session_id), other)

    def assert_pilot_refuses_retained_managed_session(self) -> None:
        before = self.service.state.get_session(self.session.session_id)
        topic_before = self.service.state.get_topic(self.topic.topic_id)
        config = replace(
            self.service.config,
            codex_permission_profile=None,
            agents=(
                replace(self.service.config.agents[0], token_file=self.project.root / "token"),
            ),
        )
        with (
            patch(
                "hermes_codex_router.pilot.CodexAppServerSupervisor",
                side_effect=AssertionError("pilot created a provider before refusing"),
            ) as supervisor,
            patch("hermes_codex_router.pilot.TelegramBotApi") as telegram,
        ):
            with self.assertRaisesRegex(ValueError, "managed Codex session"):
                run_codex_pilot(
                    config,
                    project_id=self.project.project_id,
                    chat_id=self.topic.chat_id,
                    thread_id=self.topic.thread_id,
                    topic_title="Changed pilot title",
                )
        supervisor.assert_not_called()
        telegram.assert_not_called()
        self.assertEqual(self.service.state.get_session(self.session.session_id), before)
        self.assertEqual(self.service.state.get_topic(self.topic.topic_id), topic_before)

    def test_terminal_rechecks_selected_session_before_writer_transfer(self) -> None:
        self.service.terminal = Mock()
        self.service.config = replace(
            self.service.config, codex_permission_profile=None, dispatch_mode="inline"
        )
        managed = self.service.state.get_session(self.session.session_id)
        legacy = replace(managed, codex_permission_profile=None)
        with (
            patch.object(self.service.state, "active_session", return_value=legacy),
            patch.object(self.service, "_ensure_codex_session", return_value=managed) as selected,
            patch.object(self.service, "_queue_enabled", return_value=False),
            patch.object(self.service.state, "set_writer_mode") as transfer,
            patch.object(self.service, "_ensure_provider_thread") as preparation,
        ):
            self.assertTrue(self.service.handle_update(fixtures.update(1, "/terminal")))
        selected.assert_called_once()
        transfer.assert_not_called()
        preparation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
