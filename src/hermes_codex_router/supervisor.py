from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .codex_appserver import (
    CodexAppServerClient,
    StdioJsonLineTransport,
    UnixWebSocketTransport,
)


class AppServerError(RuntimeError):
    pass


class CodexAppServerSupervisor:
    def __init__(
        self,
        socket_path: Path,
        *,
        manage_process: bool = True,
        stdio_executable: Path | None = None,
    ) -> None:
        self.socket_path = socket_path.expanduser().resolve()
        self.manage_process = manage_process
        self.stdio_executable = (
            stdio_executable.expanduser().resolve(strict=True) if stdio_executable else None
        )
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, *, timeout: float = 15.0) -> None:
        if self.stdio_executable is not None:
            return
        if not self.manage_process:
            if not self.socket_path.is_socket():
                raise AppServerError("shared Codex app-server socket is unavailable")
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.parent.chmod(0o700)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.process = subprocess.Popen(
            ("codex", "app-server", "--listen", f"unix://{self.socket_path}"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AppServerError("Codex app-server exited during startup")
            if self.socket_path.exists():
                os.chmod(self.socket_path, 0o600)
                return
            time.sleep(0.05)
        self.stop()
        raise AppServerError("Codex app-server socket did not appear")

    def client(self) -> CodexAppServerClient:
        if self.stdio_executable is not None:
            client = CodexAppServerClient(StdioJsonLineTransport.start(str(self.stdio_executable)))
            client.initialize()
            return client
        if self.manage_process and (self.process is None or self.process.poll() is not None):
            raise AppServerError("Codex app-server is not started")
        if not self.manage_process and not self.socket_path.is_socket():
            raise AppServerError("shared Codex app-server socket is unavailable")
        client = CodexAppServerClient(UnixWebSocketTransport(self.socket_path))
        client.initialize()
        return client

    def stop(self) -> None:
        if self.stdio_executable is not None:
            return
        if not self.manage_process:
            return
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
