from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.recovery_plane import RecoveryPlaneProbe, probe_recovery_plane


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


if __name__ == "__main__":
    unittest.main()
