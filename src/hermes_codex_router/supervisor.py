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
        self.transport_mode: str | None = None

    def start(self, *, timeout: float = 15.0) -> None:
        if not self.manage_process and self.socket_path.is_socket():
            self.transport_mode = "socket"
            return
        if self.stdio_executable is not None:
            self.transport_mode = "stdio-fallback"
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
                self.transport_mode = "managed-socket"
                return
            time.sleep(0.05)
        self.stop()
        raise AppServerError("Codex app-server socket did not appear")

    def client(self) -> CodexAppServerClient:
        if self.transport_mode is None:
            self.start()
        if self.transport_mode == "stdio-fallback":
            assert self.stdio_executable is not None
            client = CodexAppServerClient(StdioJsonLineTransport.start(str(self.stdio_executable)))
            client.initialize()
            return client
        if self.transport_mode == "managed-socket" and (
            self.process is None or self.process.poll() is not None
        ):
            raise AppServerError("Codex app-server is not started")
        if self.transport_mode == "socket" and not self.socket_path.is_socket():
            raise AppServerError("shared Codex app-server socket is unavailable")
        try:
            client = CodexAppServerClient(UnixWebSocketTransport(self.socket_path))
            client.initialize()
            return client
        except Exception:
            if self.stdio_executable is None or self.transport_mode == "managed-socket":
                raise
            self.transport_mode = "stdio-fallback"
            fallback = CodexAppServerClient(
                StdioJsonLineTransport.start(str(self.stdio_executable))
            )
            fallback.initialize()
            return fallback

    def stop(self) -> None:
        if self.transport_mode == "stdio-fallback":
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
        self.transport_mode = None
