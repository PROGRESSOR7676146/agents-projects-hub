"""Owned O_PATH descriptors for explicit Linux mount sources.

Descriptors pin inodes, not directory contents or trust in their owner. They
must only be inherited by bubblewrap, which consumes the bind descriptors.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType


class MountPinError(ValueError):
    """A source cannot be pinned without following or replacing a path."""


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


@dataclass(frozen=True)
class _Pin:
    path: Path
    identity: tuple[int, int, int]
    mount: int


class MountPins:
    def __init__(self) -> None:
        self._pins: dict[int, _Pin] = {}
        self._closed = False

    def __enter__(self) -> MountPins:
        self._require_open()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise MountPinError("mount descriptors are closed")

    @staticmethod
    def _walk(parent: int, parts: tuple[str, ...], directory: bool | None) -> int:
        """Use a fresh descriptor for each component; never resolve a link."""
        current = os.dup(parent)
        try:
            for index, part in enumerate(parts):
                flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
                if index < len(parts) - 1 or directory is True:
                    flags |= os.O_DIRECTORY
                following = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = following
                if stat.S_ISLNK(os.fstat(current).st_mode):
                    raise MountPinError("mount source contains a symlink")
            info = os.fstat(current)
            if directory is False and not stat.S_ISREG(info.st_mode):
                raise MountPinError("mount source is not a regular file")
            result, current = current, -1
            return result
        finally:
            if current >= 0:
                os.close(current)

    @classmethod
    def _open_absolute(cls, path: Path, directory: bool | None = None) -> int:
        if path.anchor != "/" or ".." in path.parts:
            raise MountPinError("mount source must be an absolute canonical path")
        root = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            return cls._walk(root, path.parts[1:], directory)
        finally:
            os.close(root)

    def _adopt(self, fd: int, path: Path) -> int:
        try:
            info = _identity(os.fstat(fd))
            selected_mount = mount_id(fd)
            for existing, pin in self._pins.items():
                if pin.path == path:
                    if pin.identity != info or pin.mount != selected_mount:
                        raise MountPinError("mount source identity changed")
                    return existing
            self._pins[fd] = _Pin(path, info, selected_mount)
            return fd
        finally:
            if fd not in self._pins:
                os.close(fd)

    def open(self, path: Path, *, directory: bool | None = None) -> int:
        self._require_open()
        try:
            return self._adopt(self._open_absolute(path, directory), path)
        except (OSError, AttributeError) as exc:
            raise MountPinError("mount source cannot be pinned") from exc

    def open_relative(self, parent: int, name: str, *, directory: bool | None = None) -> int:
        self._require_open()
        relative = Path(name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise MountPinError("invalid relative mount source")
        try:
            path = self._pins[parent].path / relative
            return self._adopt(self._walk(parent, relative.parts, directory), path)
        except (OSError, KeyError) as exc:
            raise MountPinError("relative mount source cannot be pinned") from exc

    def recheck(self) -> None:
        """Require the named sources to still designate the validated inodes."""
        self._require_open()
        for fd, pin in self._pins.items():
            candidate = -1
            try:
                candidate = self._open_absolute(pin.path)
                if (
                    _identity(os.fstat(candidate)) != pin.identity
                    or _identity(os.fstat(fd)) != pin.identity
                    or mount_id(candidate) != pin.mount
                    or mount_id(fd) != pin.mount
                ):
                    raise MountPinError("mount source identity changed")
            except OSError as exc:
                raise MountPinError("mount source cannot be rechecked") from exc
            finally:
                if candidate >= 0:
                    os.close(candidate)

    def mount_id(self, fd: int) -> int:
        self._require_open()
        if fd not in self._pins:
            raise MountPinError("mount source is not owned")
        return mount_id(fd)

    @property
    def pass_fds(self) -> tuple[int, ...]:
        self._require_open()
        return tuple(self._pins)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in self._pins:
            try:
                os.close(fd)
            except OSError:
                continue  # Finish cleanup even if one descriptor was already closed.
        self._pins.clear()


def mount_id(fd: int) -> int:
    """Read the kernel mount identity of an already-open descriptor."""
    try:
        fields = [
            line.partition(":")[2].strip()
            for line in Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="utf-8").splitlines()
            if line.startswith("mnt_id:")
        ]
        if len(fields) != 1 or not fields[0].isascii() or not fields[0].isdigit():
            raise MountPinError("mount identity unavailable")
        identity = int(fields[0])
        if identity <= 0:
            raise MountPinError("mount identity unavailable")
        return identity
    except OSError as exc:
        raise MountPinError("mount identity unavailable") from exc


@dataclass
class SandboxLaunch:
    argv: tuple[str, ...]
    environment: dict[str, str]
    pins: MountPins

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return self.pins.pass_fds

    def __enter__(self) -> SandboxLaunch:
        self.pins.__enter__()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.pins.close()
