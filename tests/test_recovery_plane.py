from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.recovery_plane import (
    RecoveryPlaneProbe,
    probe_recovery_plane,
    probe_tlive_runtime,
)


class RecoveryPlaneTests(unittest.TestCase):
    def test_reports_each_channel_independently(self) -> None:
        calls: list[tuple[str, ...]] = []

        def active(argv: tuple[str, ...]) -> bool:
            calls.append(argv)
            return argv[-1] == "hermes-gateway.service"

        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir)
            hermes = base / "hermes.yaml"
            tlive = base / "tlive.json"
            hermes.write_text("model: test\n", encoding="utf-8")
            tlive.write_text("{}\n", encoding="utf-8")
            hermes.chmod(0o600)
            tlive.chmod(0o600)
            result = probe_recovery_plane(
                RecoveryPlaneProbe(
                    hermes_service="hermes-gateway.service",
                    tlive_service="tlive.service",
                    hermes_config_path=hermes,
                    tlive_config_path=tlive,
                ),
                service_active=active,
                command_available=lambda command: command in {"hermes", "tlive"},
            )

        self.assertTrue(result.hermes_ok)
        self.assertFalse(result.tlive_ok)
        self.assertTrue(result.available)
        self.assertEqual(len(calls), 2)

    def test_private_configuration_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config = Path(tempdir) / "config.json"
            config.write_text("{}\n", encoding="utf-8")
            config.chmod(0o644)
            result = probe_recovery_plane(
                RecoveryPlaneProbe(
                    hermes_service="hermes-gateway.service",
                    tlive_service="tlive.service",
                    hermes_config_path=config,
                    tlive_config_path=config,
                ),
                service_active=lambda argv: True,
                command_available=lambda command: True,
            )
        self.assertFalse(result.hermes_ok)
        self.assertFalse(result.tlive_ok)
        self.assertFalse(result.available)

    def test_fresh_external_liveness_can_replace_systemd_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir)
            hermes = base / "hermes.yaml"
            tlive = base / "tlive.json"
            hermes.write_text("model: test\n", encoding="utf-8")
            tlive.write_text("{}\n", encoding="utf-8")
            hermes.chmod(0o600)
            tlive.chmod(0o600)
            result = probe_recovery_plane(
                RecoveryPlaneProbe(
                    hermes_service="hermes-gateway.service",
                    tlive_service="tlive.service",
                    hermes_config_path=hermes,
                    tlive_config_path=tlive,
                ),
                service_active=lambda argv: False,
                command_available=lambda command: command in {"hermes", "tlive"},
                hermes_liveness=True,
            )

        self.assertTrue(result.hermes_ok)
        self.assertFalse(result.tlive_ok)
        self.assertTrue(result.available)
        self.assertIn("heartbeat=healthy", result.details["hermes"])
        self.assertIn("service=inactive", result.details["hermes"])

    def test_tlive_status_can_replace_systemd_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir)
            hermes = base / "hermes.yaml"
            tlive = base / "tlive.json"
            hermes.write_text("model: test\n", encoding="utf-8")
            tlive.write_text("{}\n", encoding="utf-8")
            hermes.chmod(0o600)
            tlive.chmod(0o600)
            result = probe_recovery_plane(
                RecoveryPlaneProbe(
                    hermes_service="hermes-gateway.service",
                    tlive_service="tlive.service",
                    hermes_config_path=hermes,
                    tlive_config_path=tlive,
                ),
                service_active=lambda argv: False,
                command_available=lambda command: command in {"hermes", "tlive"},
                tlive_liveness=True,
            )

        self.assertFalse(result.hermes_ok)
        self.assertTrue(result.tlive_ok)
        self.assertTrue(result.available)
        self.assertIn("runtime=healthy", result.details["tlive"])
        self.assertIn("service=inactive", result.details["tlive"])

    def test_tlive_runtime_uses_only_bounded_status_markers(self) -> None:
        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv, ("tlive", "status"))
            return subprocess.CompletedProcess(
                argv,
                1,
                "daemon: running (pid 42)\nchannels: telegram\nweb: secret-token\n",
                "",
            )

        self.assertTrue(probe_tlive_runtime(run=run))


if __name__ == "__main__":
    unittest.main()
