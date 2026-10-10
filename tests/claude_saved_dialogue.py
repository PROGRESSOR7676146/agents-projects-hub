"""Bounded synthetic dialogue snapshots, never production authorization.

The caller owns the fixture path and expected dialogue. This observes a stable
snapshot; it cannot freeze the store, prove editor readiness or authorize exit.
"""

from __future__ import annotations

import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Literal

from tests.claude_native_request_contract import (
    MAX_REQUEST_BYTES,
    SUPPORTED_VERSION,
    NativeRequestContractError,
    _strict_json,
)

Status = Literal["waiting", "ready", "invalid"]
MAX_FILE_BYTES = 1024 * 1024
MAX_RECORDS = 512
MAX_COMPONENTS = 32
CHAIN_TYPES = frozenset({"user", "assistant", "attachment"})
METADATA_TYPES = frozenset({"queue-operation", "atis-latch", "last-prompt", "cost-state"})


@dataclass(frozen=True)
class ExpectedDialogue:
    session_id: str = field(repr=False)
    root: str = field(repr=False)
    messages: tuple[tuple[str, str], ...] = field(repr=False)


def _uuid(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
        is not None
    )


def _parts(value: object) -> tuple[str, ...] | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or not value.startswith("/")
        or "\0" in value
    ):
        return None
    try:
        if len(value.encode("utf-8")) > 4096:
            return None
    except UnicodeError:
        return None
    if value == "/":
        return ()
    parts = tuple(value[1:].split("/"))
    if len(parts) > MAX_COMPONENTS or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def _valid_expected(expected: ExpectedDialogue) -> bool:
    if (
        type(expected) is not ExpectedDialogue
        or not _uuid(expected.session_id)
        or _parts(expected.root) is None
        or type(expected.messages) is not tuple
        or len(expected.messages) not in {2, 4, 6}
    ):
        return False
    for index, item in enumerate(expected.messages):
        if (
            type(item) is not tuple
            or len(item) != 2
            or item[0] != ("user" if index % 2 == 0 else "assistant")
            or not isinstance(item[1], str)
            or not 1 <= len(item[1]) <= MAX_REQUEST_BYTES
        ):
            return False
        try:
            if len(item[1].encode("utf-8")) > MAX_REQUEST_BYTES:
                return False
        except UnicodeError:
            return False
    return True


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
    )


def _open_walk(parts: tuple[str, ...], owned: ExitStack) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Pin every lexical ancestor without following any symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/", flags)
    owned.callback(os.close, directory)
    identities = [_identity(os.fstat(directory))]
    for name in parts[:-1]:
        directory = os.open(name, flags, dir_fd=directory)
        owned.callback(os.close, directory)
        identities.append(_identity(os.fstat(directory)))
    final = os.open(
        parts[-1],
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        dir_fd=directory,
    )
    owned.callback(os.close, final)
    return final, tuple(identities)


def _safe_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 <= info.st_size <= MAX_FILE_BYTES


def _read_bounded(descriptor: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while size <= MAX_FILE_BYTES:
        chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        chunks.append(chunk)
    return None


def _dialogue(raw: bytes, expected: ExpectedDialogue) -> Status:
    lines = raw.split(b"\n")
    fragment = lines.pop()
    if len(lines) > MAX_RECORDS or len(fragment) > MAX_REQUEST_BYTES:
        return "invalid"
    previous: str | None = None
    seen: set[str] = set()
    message_index = 0
    for line in lines:
        try:
            record = _strict_json(line)
        except NativeRequestContractError:
            return "invalid"
        if (
            not isinstance(record, dict)
            or record.get("sessionId") != expected.session_id
            or ("cwd" in record and record["cwd"] != expected.root)
            or ("isSidechain" in record and record["isSidechain"] is not False)
        ):
            return "invalid"
        kind = record.get("type")
        if not isinstance(kind, str):
            return "invalid"
        if kind in METADATA_TYPES:
            if any(key in record for key in ("message", "uuid", "parentUuid")):
                return "invalid"
            continue
        identity = record.get("uuid")
        if (
            kind not in CHAIN_TYPES
            or not isinstance(identity, str)
            or not _uuid(identity)
            or identity in seen
            or "parentUuid" not in record
            or record["parentUuid"] != previous
            or record.get("cwd") != expected.root
            or record.get("isSidechain") is not False
            or record.get("version") != SUPPORTED_VERSION
        ):
            return "invalid"
        seen.add(identity)
        previous = identity
        if kind == "attachment":
            if "message" in record:
                return "invalid"
            continue
        if message_index >= len(expected.messages):
            return "invalid"
        role, text = expected.messages[message_index]
        message = record.get("message")
        content = text if role == "user" else [{"type": "text", "text": text}]
        if (
            kind != role
            or not isinstance(message, dict)
            or message.get("role") != role
            or message.get("content") != content
        ):
            return "invalid"
        message_index += 1
    if fragment or message_index < len(expected.messages):
        return "waiting"
    return "ready"


def _unchanged(
    parts: tuple[str, ...],
    descriptor: int,
    before: os.stat_result,
    ancestors: tuple[tuple[int, int], ...],
) -> bool:
    """Rewalk the name after parsing; retained FDs alone miss replacements."""
    if _signature(os.fstat(descriptor)) != _signature(before):
        return False
    try:
        with ExitStack() as owned:
            current, current_ancestors = _open_walk(parts, owned)
            return (
                current_ancestors == ancestors
                and _signature(os.fstat(current)) == _signature(before)
                and _signature(os.fstat(descriptor)) == _signature(before)
            )
    except OSError:
        # A previously readable name becoming unavailable is not a stable
        # snapshot. The caller's existing finite polling deadline owns retries.
        return False


def inspect_saved_dialogue(path: str, expected: ExpectedDialogue) -> Status:
    """Return only a fixed status, never transcript, identity or raw errors."""
    if not _valid_expected(expected):
        return "invalid"
    parts = _parts(path)
    if not parts or parts[-1] != expected.session_id + ".jsonl":
        return "invalid"
    try:
        with ExitStack() as owned:
            descriptor, ancestors = _open_walk(parts, owned)
            before = os.fstat(descriptor)
            if not _safe_file(before):
                return "invalid"
            raw = _read_bounded(descriptor)
            status: Status = "invalid" if raw is None else _dialogue(raw, expected)
            if not _unchanged(parts, descriptor, before, ancestors):
                return "waiting"
            return status
    except FileNotFoundError:
        return "waiting"
    except OSError:
        return "invalid"
