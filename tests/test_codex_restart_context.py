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
from hermes_codex_router.unix_listener_custody import ProcessIdentity


class CodexRestartContextTests(unittest.TestCase):
    def test_current_process_owns_listener_and_is_reported_as_dependent(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory) / "server.sock"
            listener.bind(str(path))
            listener.listen(1)
            self.assertEqual(context.inspect(path, os.getpid()), "dependent")
            alias = Path(directory) / "alias.sock"
            alias.symlink_to(path)
            self.assertEqual(context.inspect(alias, os.getpid()), "dependent")

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
            with patch.object(context, "verify_listener", side_effect=[None, ValueError]):
                self.assertEqual(context.inspect(path, os.getpid()), "unknown")

    def test_parent_newer_than_child_is_not_accepted_as_ancestry(self):
        child = ProcessIdentity(10, "example-boot", 100, "/example", "example", 1000)
        parent = ProcessIdentity(20, "example-boot", 101, "/example", "example", 1000)
        identities = {10: child, 20: parent}
        with (
            patch.object(context.os, "getpid", return_value=10),
            patch.object(context, "process_identity", side_effect=lambda pid, **_: identities[pid]),
            patch.object(context, "_parent_pid", return_value=20),
        ):
            with self.assertRaisesRegex(ValueError, "parent is newer"):
                context._ancestry_contains(Path("/proc"), 30)

    def test_procfs_with_a_different_pid_mapping_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "self").symlink_to("122")
            with patch.object(context.os, "getpid", return_value=123):
                with self.assertRaisesRegex(ValueError, "PID numbering"):
                    context._require_matching_pid_namespace(proc, 456)

    def test_inspect_fails_closed_when_pid_namespace_guard_fails(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory) / "server.sock"
            listener.bind(str(path))
            listener.listen(1)
            with patch.object(context, "_require_matching_pid_namespace", side_effect=ValueError):
                self.assertEqual(context.inspect(path, os.getpid()), "unknown")

    def test_listener_in_a_different_pid_namespace_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "123/ns").mkdir(parents=True)
            (proc / "456/ns").mkdir(parents=True)
            (proc / "self").symlink_to("123")
            (proc / "123/ns/pid").symlink_to("pid:[1]")
            (proc / "456/ns/pid").symlink_to("pid:[2]")
            with patch.object(context.os, "getpid", return_value=123):
                with self.assertRaisesRegex(ValueError, "PID namespace"):
                    context._require_matching_pid_namespace(proc, 456)

    def test_unknown_cli_result_does_not_claim_success(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_codex_router.codex_restart_context",
                "/example/missing.sock",
                "123",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), "unknown")

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
