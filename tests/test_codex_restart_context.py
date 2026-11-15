from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import codex_restart_context as context


class CodexRestartContextTests(unittest.TestCase):
    def test_current_process_owns_listener_and_is_reported_as_dependent(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory) / "server.sock"
            listener.bind(str(path))
            listener.listen(1)
            self.assertEqual(context.inspect(path, os.getpid()), "dependent")

    def test_child_identifies_listener_owned_by_its_parent(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory) / "server.sock"
            listener.bind(str(path))
            listener.listen(1)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "hermes_codex_router.codex_restart_context",
                    str(path),
                    str(os.getpid()),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "dependent")

    def test_foreign_process_or_unavailable_evidence_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory) / "server.sock"
            listener.bind(str(path))
            listener.listen(1)
            self.assertEqual(context.inspect(path, 99999999), "unknown")
            with patch.object(context, "verify_listener", side_effect=PermissionError):
                self.assertEqual(context.inspect(path, os.getpid()), "unknown")
            with patch.object(context, "_ancestry_contains", side_effect=PermissionError):
                self.assertEqual(context.inspect(path, os.getpid()), "unknown")

    def test_independent_is_only_an_observation_not_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.sock"
            program = (
                "import socket,sys; s=socket.socket(socket.AF_UNIX); "
                "s.bind(sys.argv[1]); s.listen(1); print('ready',flush=True); "
                "sys.stdin.read(1)"
            )
            with subprocess.Popen(
                [sys.executable, "-c", program, str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as child:
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline().strip(), "ready")
                try:
                    self.assertIn(context.inspect(path, child.pid), {"not_observed", "unknown"})
                    with patch.object(context, "_ancestry_contains", return_value=False):
                        self.assertEqual(context.inspect(path, child.pid), "not_observed")
                finally:
                    child.communicate("x", timeout=10)


if __name__ == "__main__":
    unittest.main()
