from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.hub_config import HubConfig, TerminalSettings
from hermes_codex_router.monitoring import run_monitor_once
from hermes_codex_router.release_identity import CURRENT_RELEASE
from hermes_codex_router.runtime_health import MONITOR_INSTANCE_ID
from hermes_codex_router.state import HubState


class MonitorHealthTests(unittest.TestCase):
    def _config(self, directory: str) -> HubConfig:
        root = Path(directory)
        return HubConfig(
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

    def test_monitor_cycle_publishes_completed_runtime_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            with patch(
                "hermes_codex_router.monitoring.run_doctor",
                return_value={"ok": True, "checks": []},
            ):
                run_monitor_once(config, notify=False)

            state = HubState.open(config.state_path)
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

            state = HubState.open(config.state_path)
            try:
                health = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
                assert health is not None
                self.assertEqual(health.error_code, "monitor_cycle_error")
                self.assertNotIn("private failure detail", str(health))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
