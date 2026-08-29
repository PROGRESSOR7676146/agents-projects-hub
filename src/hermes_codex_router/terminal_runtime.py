from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Sequence

from .terminal import build_codex_remote_argv


Run = Callable[..., subprocess.CompletedProcess[str]]


class TerminalRuntimeError(RuntimeError):
    pass


class TerminalRuntime:
    def __init__(
        self,
        *,
        socket_path: Path,
        distro: str = "Ubuntu",
        wt_path: Path = Path("wt.exe"),
        run: Run = subprocess.run,
    ) -> None:
        self.socket_path = socket_path.expanduser().resolve()
        self.distro = distro
        self.wt_path = wt_path
        self._run = run

    def is_running(self, name: str) -> bool:
        result = self._run(
            ("tmux", "has-session", "-t", name),
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def start(self, *, name: str, title: str, thread_id: str, cwd: Path) -> None:
        if self.is_running(name):
            raise TerminalRuntimeError("terminal takeover is already running")
        codex = build_codex_remote_argv(
            socket_path=self.socket_path,
            thread_id=thread_id,
            cwd=cwd,
        )
        self._run(
            ("tmux", "new-session", "-d", "-s", name, "-c", str(cwd), "--", *codex),
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            self._run(
                (
                    str(self.wt_path),
                    "-w",
                    "0",
                    "new-tab",
                    "--title",
                    title,
                    "wsl.exe",
                    "-d",
                    self.distro,
                    "--cd",
                    str(cwd),
                    "tmux",
                    "attach-session",
                    "-t",
                    name,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            self.release(name)
            raise

    def release(self, name: str) -> None:
        self._run(
            ("tmux", "kill-session", "-t", name),
            check=False,
            capture_output=True,
            text=True,
        )
