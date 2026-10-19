"""Default-off Linux control endpoint. Receipts concern tracked operations only.

The listener never touches SQLite, provider clients, job/approval policy or the
worker stop event. Local same-UID authority is assumed, not hostile-UID isolation.
"""

from __future__ import annotations

import os
import re
import socket
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .unix_peer import _peer_credentials
from .worker_activity_barrier import WorkerActivityBarrier
from .worker_drain_protocol import (
    FRAME_LIMIT,
    MAX_REQUEST_LIFETIME_NS,
    ControlBinding,
    ControlError,
    current_binding,
    decode_frame,
    encode_frame,
    request_digest,
    validate_request,
)

_IO_SECONDS = 0.5


class DrainableWorker(Protocol):
    activity: WorkerActivityBarrier
    _process_start_marker: str


def _peer_identity(connection: socket.socket) -> tuple[int, int]:
    pid, uid, _ = _peer_credentials(connection)
    if pid <= 0 or uid != os.geteuid():
        raise ControlError("untrusted_peer")
    return pid, uid


def _inode(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _PrivateSocketPath:
    """Pin the private parent; all socket operations use that directory FD."""

    def __init__(self, path: Path) -> None:
        if (
            not path.is_absolute()
            or path.name in (".", "..")
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", path.name) is None
            or path.parent.resolve(strict=True) != path.parent
        ):
            raise ControlError("unsafe_socket_path")
        self.path = path
        self.fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self.info = os.fstat(self.fd)
            self.check_parent()
        except BaseException:
            os.close(self.fd)
            raise

    @property
    def address(self) -> str:
        return f"/proc/self/fd/{self.fd}/{self.path.name}"

    def check_parent(self) -> None:
        info = self.path.parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o777 != 0o700
            or _inode(info) != _inode(self.info)
            or self.path.parent.resolve(strict=True) != self.path.parent
        ):
            raise ControlError("socket_directory_changed")

    def socket_info(self) -> os.stat_result:
        self.check_parent()
        info = os.stat(self.path.name, dir_fd=self.fd, follow_symlinks=False)
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o777 != 0o600
        ):
            raise ControlError("unsafe_socket")
        return info

    def close(self) -> None:
        os.close(self.fd)


def _receive_frame(connection: socket.socket) -> dict[str, object]:
    deadline = time.monotonic() + _IO_SECONDS
    data = bytearray()
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError("control_timeout")
        connection.settimeout(remaining)
        chunk = connection.recv(FRAME_LIMIT + 1 - len(data))
        if not chunk:
            raise ControlError("control_response_unknown")
        data.extend(chunk)
        if len(data) > FRAME_LIMIT:
            raise ControlError("frame_too_large")
    return decode_frame(bytes(data))


def _send_frame(connection: socket.socket, response: dict[str, object]) -> None:
    connection.settimeout(_IO_SECONDS)
    connection.sendall(encode_frame(response))


class WorkerControlEndpoint:
    def __init__(
        self, path: Path, binding: ControlBinding, activity: WorkerActivityBarrier
    ) -> None:
        self.path, self.binding, self.activity = path, binding, activity
        self._directory: _PrivateSocketPath | None = None
        self._socket: socket.socket | None = None
        self._socket_inode: tuple[int, int] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._request: tuple[str, str] | None = None
        self.unconfirmed_replies = 0

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._directory is not None or self._stop.is_set():
            raise ControlError("control_start_already_attempted")
        try:
            self.binding.require_live_process(os.getpid())
            self._directory = directory = _PrivateSocketPath(self.path)
            self._socket = listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # No automatic stale-path cleanup, even under the worker-slot lock.
            listener.bind(directory.address)
            info = os.stat(self.path.name, dir_fd=directory.fd, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise ControlError("socket_binding_changed")
            self._socket_inode = _inode(info)
            os.chmod(self.path.name, 0o600, dir_fd=directory.fd, follow_symlinks=False)
            if _inode(directory.socket_info()) != self._socket_inode:
                raise ControlError("socket_binding_changed")
            listener.listen(4)
            listener.settimeout(0.1)
            # Deliberately not an activity.thread(): it owns no provider/state pass.
            self._thread = threading.Thread(
                target=self._serve, name="worker-drain-control", daemon=True
            )
            self._thread.start()
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise ControlError("control_start_failed") from None

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                try:
                    _peer_identity(connection)
                    request = _receive_frame(connection)
                    response = self._dispatch(request)
                    _send_frame(connection, response)
                except (OSError, ValueError, RuntimeError):
                    # A dropped reply never undoes an accepted drain. No untrusted
                    # fields or exception details are reflected in error output.
                    try:
                        _send_frame(
                            connection, {"version": 1, "ok": False, "reason": "unconfirmed"}
                        )
                    except (OSError, ValueError, RuntimeError):
                        self.unconfirmed_replies = min(self.unconfirmed_replies + 1, 2**31 - 1)

    def _dispatch(self, request: dict[str, object]) -> dict[str, object]:
        validate_request(request)
        if self._stop.is_set() or self._directory is None:
            raise ControlError("control_stopping")
        if _inode(self._directory.socket_info()) != self._socket_inode:
            raise ControlError("socket_binding_changed")
        operation = request["operation"]
        if operation != "inspect":
            if request["binding"] != self.binding.as_dict():
                raise ControlError("incarnation_changed")
            digest = (
                request_digest(request) if operation == "drain" else str(request["request_digest"])
            )
            key = (str(request["request_id"]), digest)
            if self._request is not None and self._request != key:
                raise ControlError("request_conflict")
            if self._request is None:
                if operation != "drain":
                    raise ControlError("request_not_accepted")
                expires_ns = request["expires_ns"]
                assert type(expires_ns) is int
                if not 0 < expires_ns - time.monotonic_ns() <= MAX_REQUEST_LIFETIME_NS:
                    raise ControlError("request_expired")
                # One listener serializes acceptance. Keep the key if closure or
                # its reply is interrupted; expiry/disconnect never resumes work.
                self._request = key
            if operation == "drain" and not self.activity.draining:
                self.activity.request_drain()
        return {
            "version": 1,
            "ok": True,
            "binding": self.binding.as_dict(),
            "scope": "tracked_operations_only",
            "accepted": self._request is not None and self.activity.draining,
            "request_id": self._request[0] if self._request else None,
            "request_digest": self._request[1] if self._request else None,
            "operations_drained": self._request is not None and self.activity.drained,
        }

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise ControlError("control_shutdown_unconfirmed")
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._directory is not None:
            directory = self._directory
            try:
                info = os.stat(self.path.name, dir_fd=directory.fd, follow_symlinks=False)
                if stat.S_ISSOCK(info.st_mode) and _inode(info) == self._socket_inode:
                    os.unlink(self.path.name, dir_fd=directory.fd)
            except FileNotFoundError:
                pass
            finally:
                directory.close()
                self._directory = None


def _exchange(
    path: Path, request: dict[str, object], expected: ControlBinding | None
) -> dict[str, object]:
    directory: _PrivateSocketPath | None = None
    try:
        validate_request(request)
        directory = _PrivateSocketPath(path)
        inode = _inode(directory.socket_info())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(directory.address)
            pid, _ = _peer_identity(connection)
            if expected is not None:
                expected.require_live_process(pid)
                if request.get("binding") != expected.as_dict():
                    raise ControlError("request_binding_changed")
            # No retries, redirects, fallback, or replacement-process discovery.
            connection.sendall(encode_frame(request))
            response = _receive_frame(connection)
            if (
                set(response)
                != {
                    "version",
                    "ok",
                    "binding",
                    "scope",
                    "accepted",
                    "request_id",
                    "request_digest",
                    "operations_drained",
                }
                or response.get("ok") is not True
                or response.get("scope") != "tracked_operations_only"
            ):
                raise ControlError("control_response_unknown")
            binding = ControlBinding.parse(response.get("binding"))
            binding.require_live_process(pid)
            if expected is not None and binding != expected:
                raise ControlError("response_binding_changed")
            if _inode(directory.socket_info()) != inode:
                raise ControlError("socket_binding_changed")
            if (
                type(response.get("accepted")) is not bool
                or type(response.get("operations_drained")) is not bool
            ):
                raise ControlError("invalid_response")
            if response["accepted"]:
                if (
                    not isinstance(response["request_id"], str)
                    or re.fullmatch(r"[0-9a-f]{32}", response["request_id"]) is None
                    or not isinstance(response["request_digest"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", response["request_digest"]) is None
                ):
                    raise ControlError("invalid_response")
            elif (
                response["request_id"] is not None
                or response["request_digest"] is not None
                or response["operations_drained"]
            ):
                raise ControlError("invalid_response")
            if request["operation"] != "inspect" and (
                not response["accepted"]
                or response.get("request_id") != request["request_id"]
                or response.get("request_digest")
                != (
                    request_digest(request)
                    if request["operation"] == "drain"
                    else request["request_digest"]
                )
            ):
                raise ControlError("request_not_confirmed")
            return response
    except (OSError, ValueError, RuntimeError):
        raise ControlError("control_unconfirmed") from None
    finally:
        if directory is not None:
            directory.close()


def inspect_control(path: Path) -> dict[str, object]:
    """Discover only. The maintenance owner must independently select/verify it."""
    return _exchange(path, {"version": 1, "operation": "inspect"}, None)


def exchange_control(
    path: Path, binding: ControlBinding, request: dict[str, object]
) -> dict[str, object]:
    return _exchange(path, request, binding)


@contextmanager
def worker_control_session(
    path: Path | None, *, worker_id: str, worker: DrainableWorker
) -> Iterator[None]:
    """CLI owns this context inside the slot lock, outside provider accounting."""
    if path is None:
        yield
        return
    endpoint = WorkerControlEndpoint(
        path, current_binding(worker_id, worker._process_start_marker), worker.activity
    )
    try:
        endpoint.start()
        yield
    finally:
        endpoint.close()
