from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .terminal import build_codex_remote_argv

Run = Callable[..., subprocess.CompletedProcess[str]]


class TerminalRuntimeError(RuntimeError):
    pass


class TerminalRuntime:
    def __init__(
        self,
        *,
        socket_path: Path,
        backend: str = "auto",
        program: str | None = None,
        distro: str = "Ubuntu",
        run: Run = subprocess.run,
    ) -> None:
        self.socket_path = socket_path.expanduser().resolve()
        self.backend = self._resolve_backend(backend)
        self.program = program
        self.distro = distro
        self._run = run

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        if backend != "auto":
            return backend
        if os.getenv("WSL_INTEROP") or os.getenv("WSL_DISTRO_NAME"):
            return "wsl"
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("linux"):
            return "linux"
        return "tmux-only"

    def launcher_program(self) -> str | None:
        if self.backend == "tmux-only":
            return None
        defaults = {"wsl": "wt.exe", "linux": "x-terminal-emulator", "macos": "osascript"}
        return self.program or defaults[self.backend]

    def launcher_available(self) -> bool:
        program = self.launcher_program()
        return program is None or shutil.which(program) is not None or Path(program).is_file()

    def _launcher_argv(self, *, name: str, title: str, cwd: Path) -> tuple[str, ...] | None:
        program = self.launcher_program()
        if program is None:
            return None
        if self.backend == "wsl":
            return (
                program,
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
            )
        if self.backend == "linux":
            return (
                program,
                "-T",
                title,
                "-e",
                "tmux",
                "attach-session",
                "-t",
                name,
            )
        if self.backend == "macos":
            return (
                program,
                "-e",
                "on run argv",
                "-e",
                "set sessionName to item 1 of argv",
                "-e",
                'tell application "Terminal" to do script "tmux attach-session -t " & '
                "quoted form of sessionName",
                "-e",
                "end run",
                name,
            )
        raise TerminalRuntimeError(f"unsupported terminal backend: {self.backend}")

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
            launcher = self._launcher_argv(name=name, title=title, cwd=cwd)
            if launcher is not None:
                self._run(
                    launcher,
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
