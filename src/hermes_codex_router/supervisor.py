from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path
from typing import BinaryIO, Callable

from .codex_appserver import (
    CodexAppServerClient,
    StdioJsonLineTransport,
    UnixWebSocketTransport,
)


class AppServerError(RuntimeError):
    pass


def _process_start_marker() -> str:
    """Return the Linux process start tick without exposing environment data."""
    try:
        fields = Path("/proc/self/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return "unknown"


class CodexAppServerSupervisor:
    def __init__(
        self,
        socket_path: Path,
        *,
        manage_process: bool = True,
        stdio_executable: Path | None = None,
        shared_socket_health: Callable[[], bool] | None = None,
    ) -> None:
        self.socket_path = socket_path.expanduser().resolve()
        self.manage_process = manage_process
        self.stdio_executable = (
            stdio_executable.expanduser().resolve(strict=True) if stdio_executable else None
        )
        self.shared_socket_health = shared_socket_health
        self.process: subprocess.Popen[bytes] | None = None
        self.transport_mode: str | None = None
        self._ownership_file: BinaryIO | None = None

    def _acquire_socket_ownership(self) -> None:
        if self._ownership_file is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.parent.chmod(0o700)
        lock_path = self.socket_path.with_name(f"{self.socket_path.name}.lock")
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise AppServerError("cannot open managed Codex socket ownership lock") from exc
        ownership_file = os.fdopen(descriptor, "a+b")
        try:
            os.fchmod(ownership_file.fileno(), 0o600)
            fcntl.flock(ownership_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            ownership_file.seek(0)
            ownership_file.truncate()
            ownership_file.write(
                f"pid={os.getpid()}\nstart={_process_start_marker()}\n".encode("ascii")
            )
            ownership_file.flush()
            os.fsync(ownership_file.fileno())
        except (BlockingIOError, OSError) as exc:
            ownership_file.close()
            raise AppServerError("managed Codex socket ownership lock is already held") from exc
        self._ownership_file = ownership_file

    def _release_socket_ownership(self) -> None:
        ownership_file = self._ownership_file
        self._ownership_file = None
        if ownership_file is None:
            return
        try:
            fcntl.flock(ownership_file.fileno(), fcntl.LOCK_UN)
        finally:
            ownership_file.close()

    def start(self, *, timeout: float = 15.0) -> None:
        if not self.manage_process and self.socket_path.is_socket():
            if self.shared_socket_health is not None and not self.shared_socket_health():
                if self.stdio_executable is None:
                    raise AppServerError("shared Codex app-server upstream is unavailable")
                self.transport_mode = "stdio-fallback"
                return
            self.transport_mode = "socket"
            return
        if self.stdio_executable is not None:
            self.transport_mode = "stdio-fallback"
            return
        if not self.manage_process:
            if not self.socket_path.is_socket():
                raise AppServerError("shared Codex app-server socket is unavailable")
            return
        self._acquire_socket_ownership()
        if os.path.lexists(self.socket_path):
            self._release_socket_ownership()
            raise AppServerError(
                "managed Codex socket path already exists; verify its owner before removal"
            )
        try:
            self.process = subprocess.Popen(
                ("codex", "app-server", "--listen", f"unix://{self.socket_path}"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            self._release_socket_ownership()
            raise
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stop()
                raise AppServerError("Codex app-server exited during startup")
            if self.socket_path.exists():
                os.chmod(self.socket_path, 0o600)
                self.transport_mode = "managed-socket"
                return
            time.sleep(0.05)
        self.stop()
        raise AppServerError("Codex app-server socket did not appear")

    def ensure_shared_socket_health(self) -> bool:
        """Return false after moving an existing shared client to safe fallback."""
        if self.transport_mode != "socket" or self.shared_socket_health is None:
            return True
        if self.shared_socket_health():
            return True
        if self.stdio_executable is None:
            raise AppServerError("shared Codex app-server upstream is unavailable")
        self.transport_mode = "stdio-fallback"
        return False

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
        if self._ownership_file is not None and os.path.lexists(self.socket_path):
            self.socket_path.unlink()
        self._release_socket_ownership()
