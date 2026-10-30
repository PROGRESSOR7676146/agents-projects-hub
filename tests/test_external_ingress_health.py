import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from hermes_codex_router.external_service import ExternalAgentService
from hermes_codex_router.hub_config import HubConfig
from hermes_codex_router.release_identity import CURRENT_RELEASE
from hermes_codex_router.state import HubState, RuntimeHealthRecord
from hermes_codex_router.telegram import TelegramError


class IngressHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        agent = SimpleNamespace(
            agent_id="antigravity",
            runtime="antigravity",
            managed_externally=False,
            token_file=self.root / "unused-token",
            executable="unused-agy",
            runtime_home=None,
            auth_route=None,
            subscription_token_by_project=(),
            telegram_username="fixture_bot",
        )
        config = cast(
            HubConfig,
            SimpleNamespace(
                require_agent=lambda _: agent,
                agents=(agent,),
                registry_path=self.root / "unused-registry",
                state_path=self.root / "state.db",
            ),
        )
        self.config = config
        with (
            patch(
                "hermes_codex_router.external_service.load_registry",
                return_value=SimpleNamespace(projects=()),
            ),
            patch("hermes_codex_router.external_service.ExternalCliAdapter") as adapter,
        ):
            self.service = ExternalAgentService(
                config, "antigravity", direct_messages_only=True, response_transport=False
            )
            self.adapter = adapter.return_value
        self.addCleanup(self.service.close)
        self.telegram = Mock()
        self.service.telegram = self.telegram

    def health(self):
        return self.service.state.get_runtime_health(
            "controller", "external-ingress-antigravity-dm"
        )

    def recorded_health(self) -> RuntimeHealthRecord:
        row = self.health()
        if row is None:
            self.fail("expected a recorded ingress health row")
        return row

    def test_poll_success_publishes_actual_pid_release_in_dm_database(self):
        observed = []

        def poll(**kwargs):
            self.assertIsNone(self.health())
            return [{"update_id": 1}]

        def handle(update):
            observed.append(self.health())
            self.service.stop()
            return True

        self.telegram.updates.side_effect = poll
        self.service.handle_update = handle
        self.service.run_forever()
        row = observed[0]
        self.assertIsNotNone(row)
        self.assertEqual(row.pid, os.getpid())
        self.assertEqual(row.runtime, "antigravity")
        self.assertEqual(row.agent_id, "antigravity")
        self.assertEqual(row.activity_state, "unknown")
        self.assertIsNone(row.error_code)
        self.assertEqual(row.release_git_sha, CURRENT_RELEASE.git_sha)
        self.assertEqual(row.release_clean, CURRENT_RELEASE.clean_tree)
        self.assertIsNotNone(row.success_at)
        self.assertTrue(row.process_start_marker)
        self.assertEqual(row.process_start_marker, self.recorded_health().process_start_marker)
        self.adapter.run_turn.assert_not_called()
        main = HubState.open(self.root / "state.db", codex_permission_profile=None)
        try:
            self.assertEqual(main.list_runtime_health(), ())
        finally:
            main.close()

    def test_idle_health_only_after_successful_poll(self):
        rows = []

        def poll(**kwargs):
            if rows:
                rows.append(self.health())
                self.service.stop()
            else:
                rows.append(self.health())
            return []

        self.telegram.updates.side_effect = poll
        self.service.run_forever()
        self.assertIsNone(rows[0])
        self.assertEqual(rows[1].activity_state, "idle")
        self.assertIsNone(rows[1].error_code)

    def test_poll_failure_cannot_leave_ready_health(self):
        def poll(**kwargs):
            raise TelegramError(
                "fixture poll failure", operation="poll", failure_class="network_dns"
            )

        rows = []

        def waited(seconds):
            rows.append(self.health())
            self.service.stop()

        self.telegram.updates.side_effect = poll
        with patch.object(self.service._stop, "wait", side_effect=waited):
            self.service.run_forever()
        self.assertEqual(rows[0].error_code, "telegram_poll_failed")
        self.assertEqual(rows[0].activity_state, "unknown")
        self.assertIsNone(rows[0].success_at)

    def test_stop_invalidates_last_health_without_database_work_in_stop(self):
        def poll(**kwargs):
            self.service.stop()
            return []

        self.telegram.updates.side_effect = poll
        self.service.handle_update = Mock()
        self.service.run_forever()
        self.service.handle_update.assert_not_called()
        self.assertEqual(self.recorded_health().error_code, "external_service_stopped")
        self.assertEqual(self.recorded_health().activity_state, "unknown")
        with patch.object(self.service.state, "upsert_runtime_health", side_effect=AssertionError):
            self.service.stop()

    def test_unexpected_poll_exit_invalidates_previously_idle_health(self):
        calls = 0

        def poll(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture exit")
            return []

        self.telegram.updates.side_effect = poll
        with self.assertRaises(OSError):
            self.service.run_forever()
        self.assertEqual(self.recorded_health().error_code, "external_service_stopped")

    def test_health_write_failure_never_blocks_poll_or_calls_provider(self):
        self.telegram.updates.side_effect = lambda **_: self.service.stop() or []
        with patch.object(self.service.state, "upsert_runtime_health", side_effect=OSError):
            self.service.run_forever()
        self.telegram.updates.assert_called_once()
        self.adapter.run_turn.assert_not_called()

    def test_update_failure_is_visible_until_next_successful_poll(self):
        observed = []
        calls = 0

        def poll(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [{"update_id": 1}]
            observed.append(self.health())
            self.service.stop()
            return []

        self.telegram.updates.side_effect = poll
        self.service.handle_update = Mock(side_effect=ValueError("fixture update"))
        self.service.run_forever()
        self.assertEqual(observed[0].error_code, "external_update_error")
        self.assertEqual(self.service.state.get_bot_offset("antigravity"), 2)

    def test_successful_poll_recovers_failed_transport_health(self):
        observed = []
        calls = 0

        def poll(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TelegramError("fixture", operation="poll", failure_class="network_dns")
            return [{"update_id": 1}]

        def handle(update):
            observed.append(self.health())
            self.service.stop()
            return True

        self.telegram.updates.side_effect = poll
        self.service.handle_update = handle
        with patch.object(self.service._stop, "wait", return_value=False):
            self.service.run_forever()
        self.assertIsNone(observed[0].error_code)
        self.assertIsNotNone(observed[0].success_at)

    def test_aborted_update_cannot_leave_ready_row_if_exit_write_fails(self):
        self.telegram.updates.return_value = [{"update_id": 1}]
        self.service.handle_update = Mock(side_effect=KeyboardInterrupt)
        original = self.service.state.upsert_runtime_health

        def publish(**kwargs):
            if kwargs.get("error_code") == "external_service_stopped":
                raise OSError("fixture exit write failed")
            return original(**kwargs)

        with patch.object(self.service.state, "upsert_runtime_health", side_effect=publish):
            with self.assertRaises(KeyboardInterrupt):
                self.service.run_forever()
        self.assertEqual(self.recorded_health().error_code, "external_update_aborted")
        self.adapter.run_turn.assert_not_called()

    def test_group_mode_does_not_publish_extra_main_controller_rows(self):
        with (
            patch(
                "hermes_codex_router.external_service.load_registry",
                return_value=SimpleNamespace(projects=()),
            ),
            patch("hermes_codex_router.external_service.ExternalCliAdapter"),
        ):
            service = ExternalAgentService(self.config, "antigravity", response_transport=False)
        try:
            service.telegram = Mock()
            service.telegram.updates.side_effect = lambda **_: service.stop() or []
            service.run_forever()
            self.assertEqual(service.state.list_runtime_health(), ())
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
