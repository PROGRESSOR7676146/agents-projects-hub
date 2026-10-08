from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router.alerts import OperationalAlert
from hermes_codex_router.catalog_refresh import CatalogRefreshResult
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    OperationalAlertSettings,
    TerminalSettings,
)
from hermes_codex_router.migrations import LATEST_SCHEMA_VERSION
from hermes_codex_router.monitoring import _operational_telegram, run_monitor_once
from hermes_codex_router.release_identity import CURRENT_RELEASE
from hermes_codex_router.runtime_health import MONITOR_INSTANCE_ID
from hermes_codex_router.state import HubState, StateError


class MonitorHealthTests(unittest.TestCase):
    def _config(self, directory: str) -> HubConfig:
        root = Path(directory)
        config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=root / "projects.json",
            state_path=root / "state.db",
            codex_socket_path=root / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(),
        )
        # The Controller creates state; the monitor only uses an existing one.
        HubState.open(config.state_path, codex_permission_profile=None).close()
        return config

    def test_monitor_never_creates_or_migrates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent" / "state.db"
            config = replace(self._config(directory), state_path=missing)
            with self.assertRaisesRegex(StateError, "state_unavailable"):
                run_monitor_once(config, notify=False)
            self.assertFalse(missing.parent.exists())

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            older = LATEST_SCHEMA_VERSION - 1
            with closing(sqlite3.connect(config.state_path)) as connection, connection:
                connection.execute(f"PRAGMA user_version = {older}")
            with self.assertRaisesRegex(StateError, "state_schema_unsupported"):
                run_monitor_once(config, notify=False)
            with closing(sqlite3.connect(config.state_path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], older)
            self.assertEqual(
                [item.name for item in Path(directory).iterdir() if "backup" in item.name],
                [],
            )

    def test_existing_state_open_checks_schema_on_its_own_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            with closing(
                HubState.open_existing(config.state_path, codex_permission_profile=None)
            ) as state:
                self.assertEqual(state.schema_version, LATEST_SCHEMA_VERSION)
                state.record_runtime_event("monitor", "info", "probe", "ok")

            # Swap in an older database after path resolution, before the connection.
            older = Path(directory) / "older.db"
            with closing(sqlite3.connect(older)) as connection, connection:
                connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION - 1}")
            original_is_file = Path.is_file

            def replace_then_check(path: Path) -> bool:
                if path == config.state_path.resolve():
                    older.replace(config.state_path)
                return original_is_file(path)

            with patch.object(Path, "is_file", replace_then_check):
                with self.assertRaisesRegex(StateError, "state_schema_unsupported"):
                    HubState.open_existing(config.state_path, codex_permission_profile=None)
            with closing(sqlite3.connect(config.state_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    LATEST_SCHEMA_VERSION - 1,
                )
            self.assertEqual(
                [item.name for item in Path(directory).iterdir() if "backup" in item.name],
                [],
            )

            # A file removed after resolution is refused rather than created.
            def remove_then_check(path: Path) -> bool:
                if path == config.state_path.resolve():
                    config.state_path.unlink()
                return True

            with patch.object(Path, "is_file", remove_then_check):
                with self.assertRaisesRegex(StateError, "state_unavailable"):
                    HubState.open_existing(config.state_path, codex_permission_profile=None)
            self.assertFalse(config.state_path.exists())

    def test_monitor_cycle_publishes_completed_runtime_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            with patch(
                "hermes_codex_router.monitoring.run_doctor",
                return_value={"ok": True, "checks": []},
            ):
                result = run_monitor_once(config, notify=False)

            self.assertEqual(
                result["reliability"],
                {
                    "accepted_requests": 0,
                    "delivered_final_results": 0,
                    "partial_outcomes": 0,
                    "uncertain_execution": 0,
                    "unresolved_uncertain_execution": 0,
                    "recovered_results": 0,
                    "queued_work": 0,
                    "stalled_provider_work": 0,
                    "pending_delivery": 0,
                    "pending_progress_delivery": 0,
                    "unknown_delivery": 0,
                    "unknown_progress_delivery": 0,
                    "outstanding_delivery_holds": 0,
                    "released_delivery_holds": 0,
                    "oldest_queue_age_seconds": None,
                    "oldest_delivery_age_seconds": None,
                    "oldest_progress_delivery_age_seconds": None,
                    "last_delivery_delay_seconds": None,
                },
            )

            state = HubState.open(config.state_path, codex_permission_profile=None)
            try:
                health = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
                assert health is not None
                self.assertIsNotNone(health.success_at)
                self.assertIsNone(health.error_code)
                self.assertEqual(health.release_version, CURRENT_RELEASE.package_version)
            finally:
                state.close()

    def test_monitor_failure_is_published_without_exception_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            with (
                patch(
                    "hermes_codex_router.monitoring.refresh_provider_catalogs",
                    side_effect=RuntimeError("private failure detail"),
                ),
                self.assertRaises(RuntimeError),
            ):
                run_monitor_once(config, notify=False)

            state = HubState.open(config.state_path, codex_permission_profile=None)
            try:
                health = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
                assert health is not None
                self.assertEqual(health.error_code, "monitor_cycle_error")
                self.assertNotIn("private failure detail", str(health))
            finally:
                state.close()

    def test_monitor_does_not_emit_context_size_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions_dir = Path(directory) / "sessions"
            sessions_dir.mkdir()
            (sessions_dir / "rollout-example.jsonl").write_text(
                '{"type":"token_usage_record","payload":{"session_id":"example-session",'
                '"usage":{"input_tokens":75000,"total_tokens":75500}}}\n',
                encoding="utf-8",
            )
            config = replace(self._config(directory), codex_sessions_dir=sessions_dir)
            with patch(
                "hermes_codex_router.monitoring.run_doctor",
                return_value={"ok": True, "checks": []},
            ):
                result = run_monitor_once(config, notify=False)

            alerts = result["alerts"]
            self.assertIsInstance(alerts, list)
            assert isinstance(alerts, list)
            self.assertNotIn(
                "codex_context_bloat",
                {alert["code"] for alert in alerts if isinstance(alert, dict)},
            )

    def test_operational_sender_prefers_configured_hub_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_token = root / "codex.token"
            hub_token = root / "hub.token"
            codex_token.write_text("123456:codex-example", encoding="utf-8")
            hub_token.write_text("654321:hub-example", encoding="utf-8")
            codex_token.chmod(0o600)
            hub_token.chmod(0o600)
            config = replace(
                self._config(directory),
                agents=(
                    AgentDefinition(
                        "codex",
                        "Codex",
                        "example_codex_bot",
                        "codex",
                        codex_token,
                        False,
                        False,
                        "example-model",
                        "medium",
                    ),
                ),
                hub_bot=HubTelegramBot("example_hub_bot", hub_token),
            )

            with patch("hermes_codex_router.monitoring.TelegramBotApi") as api:
                _telegram, identity = _operational_telegram(config)

            api.assert_called_once_with("654321:hub-example")
            self.assertEqual(identity, "hub")

    def test_operational_sender_rejects_config_without_hub_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_token = Path(directory) / "codex.token"
            codex_token.write_text("123456:codex-example", encoding="utf-8")
            codex_token.chmod(0o600)
            config = replace(
                self._config(directory),
                agents=(
                    AgentDefinition(
                        "codex",
                        "Codex",
                        "example_codex_bot",
                        "codex",
                        codex_token,
                        False,
                        False,
                        "example-model",
                        "medium",
                    ),
                ),
            )

            with (
                patch("hermes_codex_router.monitoring.TelegramBotApi") as api,
                self.assertRaisesRegex(RuntimeError, "Hub bot is required"),
            ):
                _operational_telegram(config)

            api.assert_not_called()

    def test_general_monitor_alert_is_sent_by_hub_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hub_token = root / "hub.token"
            hub_token.write_text("654321:hub-example", encoding="utf-8")
            hub_token.chmod(0o600)
            config = replace(
                self._config(directory),
                hub_bot=HubTelegramBot("example_hub_bot", hub_token),
                operational_alerts=OperationalAlertSettings(-1001234567890, 77),
            )
            bot = Mock()

            with (
                patch(
                    "hermes_codex_router.monitoring.refresh_provider_catalogs",
                    return_value=CatalogRefreshResult((), (), {}),
                ),
                patch(
                    "hermes_codex_router.monitoring.run_doctor",
                    return_value={"ok": True, "checks": []},
                ),
                patch(
                    "hermes_codex_router.monitoring.evaluate_operational_alerts",
                    return_value=(
                        OperationalAlert("example:key", "example_alert", "warning", "Example"),
                    ),
                ),
                patch(
                    "hermes_codex_router.monitoring._operational_telegram",
                    return_value=(bot, "hub"),
                ) as sender,
            ):
                result = run_monitor_once(config, notify=True)

            sender.assert_called_once_with(config)
            bot.send_html.assert_called_once()
            self.assertEqual(result["delivered"], ["example_alert:hub"])


if __name__ == "__main__":
    unittest.main()
