"""Sealed text snapshots of a trusted caller's explicit material selection.

This primitive grants no authorization and discovers no project files. A future
workflow must supply and recheck its own durable binding before invocation.
The caller owns the capsule; send bounded bytes through stdin, not its host FD.
"""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Sequence

from .claude_mount_pins import MountPinError, MountPins
from .process_namespace import NamespaceError, _absolute_path, _not_broad, _reject_nested_mounts

_MAX_FILES = 32
_MAX_FILE_BYTES = 64 * 1024
_MAX_TOTAL_BYTES = 256 * 1024
_MAX_CAPSULE_BYTES = 1024 * 1024
# Linux UAPI values also work with Python built against older libc headers,
# which may omit these names even though the running kernel supports sealing.
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_SEALS = 0x0008 | 0x0004 | 0x0002 | 0x0001  # WRITE, GROW, SHRINK, SEAL


class ReviewMaterialError(ValueError):
    """Selected material cannot be captured or decoded safely."""


@dataclass(frozen=True)
class MaterialSelection:
    name: str
    size: int
    sha256: str


def _validate_selection(item: MaterialSelection) -> None:
    name = item.name
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 240
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or "\\" in name
        or any(part in ("", ".", "..", ".git") for part in name.split("/"))
    ):
        raise ReviewMaterialError("material name must be a bounded relative file name")
    if type(item.size) is not int or not 0 <= item.size <= _MAX_FILE_BYTES:
        raise ReviewMaterialError("material size exceeds the allowed bound")
    if not isinstance(item.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", item.sha256) is None:
        raise ReviewMaterialError("material needs an exact SHA-256 digest")


def _validate_binding(binding: object) -> None:
    if (
        not isinstance(binding, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", binding) is None
    ):
        raise ReviewMaterialError("material binding must be a bounded opaque reference")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewMaterialError("capsule contains duplicate object keys")
        result[key] = value
    return result


def _encode(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def decode_review_capsule(data: bytes, expected_digest: str) -> dict[str, Any]:
    if (
        not isinstance(data, bytes)
        or not 1 <= len(data) <= _MAX_CAPSULE_BYTES
        or not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or hashlib.sha256(data).hexdigest() != expected_digest
    ):
        raise ReviewMaterialError("capsule bytes or digest are invalid")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or set(document) != {"version", "binding", "files"}:
            raise ReviewMaterialError("capsule schema is invalid")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ReviewMaterialError("capsule version is unsupported")
        _validate_binding(document["binding"])
        entries = document["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_FILES:
            raise ReviewMaterialError("capsule file count exceeds the bound")
        names: list[str] = []
        total = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256", "text"}:
                raise ReviewMaterialError("capsule file schema is invalid")
            item = MaterialSelection(entry["name"], entry["size"], entry["sha256"])
            _validate_selection(item)
            text = entry["text"]
            if not isinstance(text, str) or "\x00" in text:
                raise ReviewMaterialError("capsule material must be UTF-8 text")
            captured = text.encode("utf-8")
            if len(captured) != item.size or hashlib.sha256(captured).hexdigest() != item.sha256:
                raise ReviewMaterialError("capsule material digest or size differs")
            names.append(item.name)
            total += item.size
        if names != sorted(set(names)) or total > _MAX_TOTAL_BYTES or _encode(document) != data:
            raise ReviewMaterialError("capsule ordering, size or encoding is invalid")
        return document
    except ReviewMaterialError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ReviewMaterialError("capsule encoding is invalid") from error


class ReviewCapsule:
    def __init__(self, fd: int, digest: str, size: int) -> None:
        if type(size) is not int or not 1 <= size <= _MAX_CAPSULE_BYTES:
            raise ReviewMaterialError("material capsule size exceeds the bound")
        self._fd, self._digest, self._size = fd, digest, size

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def size(self) -> int:
        return self._size

    def fileno(self) -> int:
        if self._fd < 0:
            raise ReviewMaterialError("material capsule is closed")
        return self._fd

    def read(self) -> bytes:
        fd = self.fileno()
        if fcntl.fcntl(fd, _F_GET_SEALS) & _SEALS != _SEALS:
            raise ReviewMaterialError("material capsule is not sealed")
        pieces: list[bytes] = []
        offset = 0
        while offset < self.size:
            part = os.pread(fd, self.size - offset, offset)
            if not part:
                raise ReviewMaterialError("material capsule is incomplete")
            pieces.append(part)
            offset += len(part)
        data = b"".join(pieces)
        decode_review_capsule(data, self.digest)
        return data

    def close(self) -> None:
        if self._fd >= 0:
            fd, self._fd = self._fd, -1
            os.close(fd)

    def __enter__(self) -> ReviewCapsule:
        self.fileno()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _read_selected(source_fd: int, size: int) -> bytes:
    # O_PATH cannot read data. Reopen its exact inode, never its mutable name.
    fd = os.open(f"/proc/self/fd/{source_fd}", os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        pieces: list[bytes] = []
        remaining = size + 1
        while remaining:
            part = os.read(fd, remaining)
            if not part:
                break
            pieces.append(part)
            remaining -= len(part)
        return b"".join(pieces)
    finally:
        os.close(fd)


def _require_descriptor_path(fd: int, expected: Path) -> None:
    # Inode/mount identity alone permits casefold aliases to excluded names.
    # Require the kernel's actual spelling, also refusing deleted/renamed pins.
    if os.readlink(f"/proc/self/fd/{fd}") != str(expected):
        raise ReviewMaterialError("selected material descriptor path differs")


def _create_sealable_memfd() -> int:
    if hasattr(os, "memfd_create"):
        return os.memfd_create("review-materials", 0x0001 | 0x0002)
    # Some supported Python builds omit the wrapper and sealing constants.
    # Use the named libc API, never architecture-dependent syscall numbers
    # and never an unsealed filesystem fallback.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        create = libc.memfd_create
    except AttributeError as error:
        raise ReviewMaterialError("Linux sealed memfd support is required") from error
    create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    create.restype = ctypes.c_int
    fd = create(b"review-materials", 0x0001 | 0x0002)
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return fd


def build_review_capsule(
    root: Path, selection: Sequence[MaterialSelection], *, binding: str
) -> ReviewCapsule:
    """Capture selected regular text files below an already authorized root."""
    _validate_binding(binding)
    if sys.platform != "linux":
        raise ReviewMaterialError("Linux sealed memfd support is required")
    if not 1 <= len(selection) <= _MAX_FILES:
        raise ReviewMaterialError("material selection count exceeds the bound")
    selection = tuple(selection)
    if not 1 <= len(selection) <= _MAX_FILES:
        raise ReviewMaterialError("material selection count changed")
    for item in selection:
        _validate_selection(item)
    if (
        len({item.name for item in selection}) != len(selection)
        or sum(item.size for item in selection) > _MAX_TOTAL_BYTES
    ):
        raise ReviewMaterialError("duplicate or oversized material selection")
    capsule_fd = -1
    try:
        root = _absolute_path(root, "authorized material root")
        _not_broad(root, "authorized material root")
        if any(part.casefold() == ".git" for part in root.parts):
            raise ReviewMaterialError("material root includes Git metadata")
        with MountPins() as pins:
            root_fd = pins.open(root, directory=True)
            _require_descriptor_path(root_fd, root)
            paths = {root_fd: root}
            root_device = os.fstat(root_fd).st_dev
            root_mount = pins.mount_id(root_fd)
            _reject_nested_mounts(root, mount_ids={root: root_mount})
            entries: list[dict[str, Any]] = []
            for item in sorted(selection, key=lambda entry: entry.name):
                fd = pins.open_relative(root_fd, item.name, directory=False)
                _require_descriptor_path(fd, root / item.name)
                paths[fd] = root / item.name
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_dev != root_device
                    or before.st_nlink != 1
                    or before.st_size != item.size
                    or pins.mount_id(fd) != root_mount
                ):
                    raise ReviewMaterialError("selected file type, size or mount is invalid")
                captured = _read_selected(fd, item.size)
                after = os.fstat(fd)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ReviewMaterialError("selected source changed during capture")
                if (
                    len(captured) != item.size
                    or hashlib.sha256(captured).hexdigest() != item.sha256
                ):
                    raise ReviewMaterialError("selected material digest or size differs")
                text = captured.decode("utf-8")
                if "\x00" in text:
                    raise ReviewMaterialError("selected material must be UTF-8 text")
                entries.append(
                    {"name": item.name, "size": item.size, "sha256": item.sha256, "text": text}
                )
            pins.recheck()
            for fd, expected in paths.items():
                _require_descriptor_path(fd, expected)
        data = _encode({"version": 1, "binding": binding, "files": entries})
        if len(data) > _MAX_CAPSULE_BYTES:
            raise ReviewMaterialError("material capsule encoding exceeds its bound")
        digest = hashlib.sha256(data).hexdigest()
        decode_review_capsule(data, digest)
        capsule_fd = _create_sealable_memfd()
        offset = 0
        while offset < len(data):
            written = os.write(capsule_fd, data[offset:])
            if written <= 0:
                raise ReviewMaterialError("material capsule write failed")
            offset += written
        fcntl.fcntl(capsule_fd, _F_ADD_SEALS, _SEALS)
        result = ReviewCapsule(capsule_fd, digest, len(data))
        capsule_fd = -1
        return result
    except (MountPinError, NamespaceError, OSError, UnicodeError) as error:
        raise ReviewMaterialError("material capture failed") from error
    finally:
        if capsule_fd >= 0:
            os.close(capsule_fd)
