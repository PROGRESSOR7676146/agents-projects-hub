from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.terminal_runtime import TerminalRuntime


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 1 if argv[:2] == ("tmux", "has-session") else 0, "", ""
        )


class TerminalRuntimeTests(unittest.TestCase):
    def test_takeover_uses_argv_and_same_remote_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            cwd = Path(tempdir) / "Pythia"
            cwd.mkdir()
            socket_path = Path(tempdir) / "codex.sock"
            fake = FakeRun()
            runtime = TerminalRuntime(socket_path=socket_path, backend="wsl", run=fake)
            runtime.start(
                name="hph-pythia-main-codex-73",
                title="Pythia - main - Codex",
                thread_id="01a049a1-ae3c-73d3-91da-98bc85a26400",
                cwd=cwd,
            )
        tmux = fake.calls[1]
        self.assertIn("--remote", tmux)
        self.assertIn(f"unix://{socket_path}", tmux)
        self.assertIn("01a049a1-ae3c-73d3-91da-98bc85a26400", tmux)
        self.assertEqual(fake.calls[2][0], "wt.exe")

    def test_tmux_only_backend_does_not_launch_a_terminal_emulator(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            cwd = Path(tempdir) / "Project"
            cwd.mkdir()
            fake = FakeRun()
            runtime = TerminalRuntime(
                socket_path=Path(tempdir) / "codex.sock",
                backend="tmux-only",
                run=fake,
            )
            runtime.start(
                name="hph-project-topic-codex-1",
                title="Project",
                thread_id="01a049a1-ae3c-73d3-91da-98bc85a26400",
                cwd=cwd,
            )
        self.assertEqual(len(fake.calls), 2)


if __name__ == "__main__":
    unittest.main()
