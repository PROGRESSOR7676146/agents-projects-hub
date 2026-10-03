"""Authenticated, human-only tlive permission receipt transport."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .claude_permission_protocol import (
    PermissionProtocolError,
    ProtectedPayload,
    canonical_uuid,
    parse_json_strict,
)


class TlivePermissionError(RuntimeError):
    """A protected decision could not be established."""


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_POSITIVE = re.compile(r"[1-9][0-9]*\Z")


@dataclass(frozen=True, slots=True)
class TlivePermissionConfig:
    socket_path: str
    owner_id: str
    chat_id: str
    request_key: bytes = field(repr=False)
    result_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class TliveCapability:
    epoch: str


@dataclass(frozen=True, slots=True)
class ProtectedDecision:
    decision: str
    actor: dict[str, Any] | None


def _safe_socket_path(path: str) -> None:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise TlivePermissionError("invalid socket path")
    current = Path("/")
    try:
        for part in Path(path).parts[1:-1]:
            current /= part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
                raise TlivePermissionError("unsafe socket parent")
            if info.st_mode & 0o022 and not (
                info.st_mode & stat.S_ISVTX and info.st_uid in (0, os.getuid())
            ):
                raise TlivePermissionError("unsafe socket parent")
        info = Path(path).lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise TlivePermissionError("unsafe socket")
    except OSError as exc:
        raise TlivePermissionError("socket unavailable") from exc


def load_tlive_permission_config(path: str | os.PathLike[str]) -> TlivePermissionConfig:
    """Read one explicitly named private file through one no-follow descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= 4096
            ):
                raise TlivePermissionError("unsafe protected configuration")
            chunks = bytearray()
            while len(chunks) <= 4096:
                block = os.read(fd, min(4097 - len(chunks), 4097))
                if not block:
                    break
                chunks.extend(block)
            if len(chunks) > 4096:
                raise TlivePermissionError("protected configuration too large")
        finally:
            os.close(fd)
        value = parse_json_strict(bytes(chunks), max_bytes=4096)
        required = {"version", "socket_path", "owner_id", "chat_id", "request_key", "result_key"}
        if (
            not isinstance(value, dict)
            or set(value) != required
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise TlivePermissionError("invalid protected configuration")
        for key in ("owner_id", "chat_id"):
            if not isinstance(value[key], str) or not _POSITIVE.fullmatch(value[key]):
                raise TlivePermissionError("invalid protected configuration")
        for key in ("request_key", "result_key"):
            if not isinstance(value[key], str) or not _HEX64.fullmatch(value[key]):
                raise TlivePermissionError("invalid protected configuration")
        if value["request_key"] == value["result_key"]:
            raise TlivePermissionError("invalid protected configuration")
        socket_path = value["socket_path"]
        if (
            not isinstance(socket_path, str)
            or not os.path.isabs(socket_path)
            or "\x00" in socket_path
        ):
            raise TlivePermissionError("invalid protected configuration")
        return TlivePermissionConfig(
            socket_path,
            value["owner_id"],
            value["chat_id"],
            bytes.fromhex(value["request_key"]),
            bytes.fromhex(value["result_key"]),
        )
    except (OSError, PermissionProtocolError, TypeError, ValueError) as exc:
        raise TlivePermissionError("protected configuration unavailable") from exc


def _mac(key: bytes, fields: list[Any]) -> str:
    data = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hmac.new(key, data, hashlib.sha256).hexdigest()


class ProtectedTliveClient:
    def __init__(self, config: TlivePermissionConfig, *, timeout: float = 3.0):
        self.config = config
        self.timeout = min(max(timeout, 0.1), 10.0)

    def _exchange(
        self, request: dict[str, Any], *, deadline: float, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        try:
            _safe_socket_path(self.config.socket_path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(min(0.25, max(0.01, deadline - time.monotonic())))
                sock.connect(self.config.socket_path)
                wire = (
                    json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + b"\n"
                )
                if len(wire) > 65536 + 4096:
                    raise TlivePermissionError("protected request too large")
                sock.sendall(wire)
                buf = bytearray()
                while True:
                    if stop is not None and stop.is_set():
                        raise TlivePermissionError("permission cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TlivePermissionError("permission timed out")
                    sock.settimeout(min(0.2, remaining))
                    try:
                        chunk = sock.recv(min(4096, 131073 - len(buf)))
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise TlivePermissionError("protected channel closed")
                    buf.extend(chunk)
                    if len(buf) > 131072:
                        raise TlivePermissionError("protected response too large")
                    if b"\n" in buf:
                        line, trailing = bytes(buf).split(b"\n", 1)
                        if trailing:
                            raise TlivePermissionError("trailing protected response")
                        value = parse_json_strict(line, max_bytes=131072)
                        if not isinstance(value, dict):
                            raise TlivePermissionError("invalid protected response")
                        return value
        except (OSError, ValueError, PermissionProtocolError) as exc:
            raise TlivePermissionError("protected channel unavailable") from exc

    def hello(self) -> TliveCapability:
        challenge = str(uuid4())
        result = self._exchange(
            {"kind": "hub.permission.hello", "version": 1, "challenge": challenge},
            deadline=time.monotonic() + self.timeout,
        )
        if (
            set(result) != {"kind", "version", "epoch", "tag"}
            or result["kind"] != "hub.permission.capability"
            or type(result["version"]) is not int
            or result["version"] != 1
        ):
            raise TlivePermissionError("invalid protected capability")
        try:
            epoch = canonical_uuid(result["epoch"])
        except PermissionProtocolError as exc:
            raise TlivePermissionError("invalid protected capability") from exc
        tag = result["tag"]
        expected = _mac(self.config.result_key, ["capability", challenge, epoch, 1])
        if (
            not isinstance(tag, str)
            or not _HEX64.fullmatch(tag)
            or not hmac.compare_digest(tag, expected)
        ):
            raise TlivePermissionError("invalid protected capability")
        return TliveCapability(epoch)

    def request(
        self, payload: str, capability: TliveCapability, stop: threading.Event | None = None
    ) -> ProtectedDecision:
        try:
            binding = ProtectedPayload.parse(payload)
            epoch = canonical_uuid(capability.epoch)
            if len(payload.encode("utf-8")) > 65536:
                raise PermissionProtocolError("oversized payload")
        except (AttributeError, PermissionProtocolError, UnicodeError) as exc:
            raise TlivePermissionError("invalid protected request") from exc
        if stop is not None and stop.is_set():
            raise TlivePermissionError("permission cancelled")
        remaining = (binding.expires_at / 1000) - time.time()
        if remaining <= 0 or remaining > 600:
            raise TlivePermissionError("invalid protected expiry")
        result = self._exchange(
            {
                "kind": "hub.permission.request",
                "epoch": epoch,
                "payload": payload,
                "tag": _mac(self.config.request_key, ["request", epoch, payload]),
            },
            deadline=time.monotonic() + remaining,
            stop=stop,
        )
        if (
            set(result) != {"kind", "decision", "epoch", "payload", "actor", "tag"}
            or result["kind"] != "hub.permission.result"
            or result["decision"] not in ("allow", "deny")
            or result["epoch"] != epoch
            or result["payload"] != payload
        ):
            raise TlivePermissionError("invalid protected result")
        tag = result["tag"]
        expected = _mac(
            self.config.result_key, ["result", epoch, payload, result["decision"], result["actor"]]
        )
        if (
            not isinstance(tag, str)
            or not _HEX64.fullmatch(tag)
            or not hmac.compare_digest(tag, expected)
        ):
            raise TlivePermissionError("invalid protected result")
        actor = result["actor"]
        if result["decision"] == "allow" and actor is None:
            raise TlivePermissionError("missing human receipt")
        if actor is not None:
            suffix = "a" if result["decision"] == "allow" else "d"
            fields = {
                "callbackId",
                "userId",
                "isBot",
                "chatId",
                "chatType",
                "messageId",
                "data",
            }
            if (
                not isinstance(actor, dict)
                or set(actor) != fields
                or not isinstance(actor["callbackId"], str)
                or not 1 <= len(actor["callbackId"]) <= 200
                or actor["userId"] != self.config.owner_id
                or actor["isBot"] is not False
                or actor["chatId"] != self.config.chat_id
                or actor["chatType"] != "private"
                or not isinstance(actor["messageId"], str)
                or not _POSITIVE.fullmatch(actor["messageId"])
                or actor["data"] != f"hp:{binding.request_nonce}:{suffix}"
            ):
                raise TlivePermissionError("invalid human receipt")
        return ProtectedDecision(result["decision"], actor)
