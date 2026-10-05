from __future__ import annotations

import unittest
from pathlib import Path


class SystemdTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def unit(self, name: str) -> str:
        return (self.root / "systemd" / name).read_text(encoding="utf-8")

    def test_worker_and_sender_have_distinct_commands(self) -> None:
        worker = self.unit("agents-projects-hub-worker@.service")
        sender = self.unit("agents-projects-hub-sender.service")

        self.assertIn("agents-projects-hub worker ", worker)
        self.assertIn("--agent %i", worker)
        codex_slot = self.unit("agents-projects-hub-codex-worker@.service")
        self.assertIn("--agent codex --slot %i", codex_slot)
        claude_slot = self.unit("agents-projects-hub-claude-worker@.service")
        self.assertIn("--agent claude --slot %i", claude_slot)
        self.assertNotIn("agents-projects-hub serve ", worker)
        self.assertIn("agents-projects-hub sender ", sender)

    def test_operational_components_do_not_require_each_other(self) -> None:
        names = (
            "agents-projects-hub.service",
            "agents-projects-hub-worker@.service",
            "agents-projects-hub-codex-worker@.service",
            "agents-projects-hub-claude-worker@.service",
            "agents-projects-hub-sender.service",
            "agents-projects-hub-project-provisioner.service",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertNotIn("Requires=", self.unit(name))

    def test_installer_copies_worker_and_sender_units(self) -> None:
        installer = (self.root / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("agents-projects-hub-worker@.service", installer)
        self.assertIn("agents-projects-hub-codex-worker@.service", installer)
        self.assertIn("agents-projects-hub-claude-worker@.service", installer)
        self.assertIn("agents-projects-hub-sender.service", installer)
        self.assertIn("agents-projects-hub-project-provisioner.service", installer)
        provisioner = self.unit("agents-projects-hub-project-provisioner.service")
        self.assertIn("agents-projects-hub project-provisioner ", provisioner)

    def test_retired_multi_auth_unit_templates_are_gone(self) -> None:
        installer = (self.root / "scripts" / "install.sh").read_text(encoding="utf-8")
        units = [
            path.relative_to(self.root).as_posix() for path in (self.root / "systemd").rglob("*")
        ]
        self.assertEqual([item for item in units if "multi-auth" in item], [])
        self.assertNotIn("multi-auth", installer)

    def test_codex_client_units_see_the_daemon_socket_through_private_tmp(self) -> None:
        # A private /tmp hides the Codex daemon socket and forces the stdio fallback.
        for name in (
            "agents-projects-hub.service",
            "agents-projects-hub-worker@.service",
            "agents-projects-hub-codex-worker@.service",
            "agents-projects-hub-monitor.service",
            "agents-projects-hub@.service",
            "tlive.service",
        ):
            with self.subTest(unit=name):
                unit = self.unit(name)
                self.assertIn("PrivateTmp=true", unit)
                self.assertIn("BindPaths=-/tmp/codex-daemon-%U", unit)

    def test_socket_bind_source_exists_before_codex_workers_start(self) -> None:
        prepare = self.unit("agents-projects-hub-codex-socket-dir.service")
        self.assertIn("Type=oneshot", prepare)
        self.assertIn("prepare-codex-socket-dir", prepare)
        for name in (
            "agents-projects-hub.service",
            "agents-projects-hub-worker@.service",
            "agents-projects-hub-codex-worker@.service",
            "agents-projects-hub-monitor.service",
            "agents-projects-hub@.service",
            "tlive.service",
        ):
            with self.subTest(unit=name):
                unit = self.unit(name)
                self.assertIn("Wants=agents-projects-hub-codex-socket-dir.service", unit)
                self.assertNotIn("Requires=agents-projects-hub-codex-socket-dir.service", unit)
                self.assertIn("After=agents-projects-hub-codex-socket-dir.service", unit)
        installer = (self.root / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("agents-projects-hub-codex-socket-dir.service", installer)

    def test_monitor_timer_schedules_from_each_activation(self) -> None:
        timer = self.unit("agents-projects-hub-monitor.timer")
        self.assertIn("OnActiveSec=5min", timer)
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertNotIn("OnBootSec=", timer)


if __name__ == "__main__":
    unittest.main()
