from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.installed_processes import observe_units


@unittest.skipUnless(sys.platform == "linux", "Linux installed runtime contract")
class InstalledProcessesTests(unittest.TestCase):
    def test_passive_readback_of_owned_native_fixture_process(self) -> None:
        unit = "agents-projects-hub-example.service"
        argv = [sys.executable, "-I", "-B", "-c", "import time; time.sleep(10)"]
        child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        calls = []

        def systemctl(*args: str) -> str:
            calls.append(args)
            if args[0] == "list-units":
                return json.dumps([{"unit": unit}])
            return str(child.pid)

        try:
            with patch("hermes_codex_router.installed_processes._systemctl", side_effect=systemctl):
                result = observe_units((unit,))
            self.assertEqual(result[unit]["argv"], argv)
            self.assertEqual(result[unit]["executable"], str(Path(sys.executable).resolve()))
            self.assertTrue(result[unit]["start_marker"].startswith(f"{child.pid}:"))
            self.assertEqual([call[0] for call in calls], ["list-units", "show", "show"])
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_restart_during_readback_refuses(self) -> None:
        unit = "agents-projects-hub-example.service"
        with (
            patch(
                "hermes_codex_router.installed_processes._systemctl",
                return_value=json.dumps([{"unit": unit}]),
            ),
            patch(
                "hermes_codex_router.installed_processes._incarnation",
                side_effect=[(os.getpid(), "one"), (os.getpid(), "two")],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "process changed"):
                observe_units((unit,))

    def test_unavailable_manager_is_not_an_empty_healthy_inventory(self) -> None:
        with patch(
            "hermes_codex_router.installed_processes.subprocess.run",
            side_effect=subprocess.TimeoutExpired("fictional", 20),
        ):
            with self.assertRaisesRegex(RuntimeError, "service manager unavailable"):
                observe_units(())
