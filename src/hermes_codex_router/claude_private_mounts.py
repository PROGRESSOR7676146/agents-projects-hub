"""Validation-only authority pins and filesystem provenance for Claude mounts.

These descriptors never belong to a provider launch. Coordinates detect bind
aliases before launch; they do not freeze contents or establish host custody.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Mapping, Sequence

from .claude_mount_pins import MountPinError, MountPins

_MAX_TABLE_BYTES = 4 * 1024 * 1024
_MAX_MOUNTS = 20_000
_MAX_PRIVATE_PATHS = 128


class PrivateMountError(ValueError):
    """Private authority cannot be excluded with current mount evidence."""


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _mount_path(raw: str) -> Path:
    if re.search(r"\\(?!040|011|012|134)", raw):
        raise PrivateMountError("private authority mount provenance is malformed")
    decoded = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), raw)
    path = Path(decoded)
    if path.anchor != "/" or ".." in path.parts or str(path) != decoded or "\x00" in decoded:
        raise PrivateMountError("private authority mount provenance is malformed")
    return path


@dataclass(frozen=True)
class _Mount:
    identity: int
    device: tuple[int, int]
    root: Path | None
    point: Path
    filesystem: str


def _read_mountinfo() -> str:
    with Path("/proc/self/mountinfo").open("rb") as stream:
        data = stream.read(_MAX_TABLE_BYTES + 1)
    if len(data) > _MAX_TABLE_BYTES:
        raise PrivateMountError("private authority mount provenance exceeds its bound")
    return data.decode("utf-8")


def _mount_table() -> dict[int, _Mount]:
    try:
        text = _read_mountinfo()
    except (OSError, UnicodeError) as exc:
        raise PrivateMountError("private authority mount provenance is unavailable") from exc
    if len(text.encode("utf-8")) > _MAX_TABLE_BYTES or len(text.splitlines()) > _MAX_MOUNTS:
        raise PrivateMountError("private authority mount provenance exceeds its bound")
    mounts: dict[int, _Mount] = {}
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        fields, tail = before.split(), after.split()
        if (
            not separator
            or len(fields) < 6
            or len(tail) < 3
            or not re.fullmatch(r"[0-9]+", fields[0])
            or not re.fullmatch(r"[0-9]+", fields[1])
            or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2])
        ):
            raise PrivateMountError("private authority mount provenance is malformed")
        identity = int(fields[0])
        if identity <= 0 or identity in mounts:
            raise PrivateMountError("private authority mount provenance is ambiguous")
        major, minor = fields[2].split(":")
        # Kernel namespace handles are legitimate unrelated mounts. They have
        # an opaque root such as net:[123], never a provider filesystem source.
        root = (
            None
            if tail[0] == "nsfs" and re.fullmatch(r"[a-z]+:\[[0-9]+\]", fields[3])
            else _mount_path(fields[3])
        )
        mounts[identity] = _Mount(
            identity, (int(major), int(minor)), root, _mount_path(fields[4]), tail[0]
        )
    if not mounts:
        raise PrivateMountError("private authority mount provenance is unavailable")
    return mounts


@dataclass(frozen=True)
class _Coordinate:
    mount: _Mount
    path: Path
    inode: tuple[int, int]


def _coordinate(path: Path, fd: int, identity: int, table: Mapping[int, _Mount]) -> _Coordinate:
    mount = table.get(identity)
    try:
        info = os.fstat(fd)
        kernel_path = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as exc:
        raise PrivateMountError("private authority descriptor path is unavailable") from exc
    if (
        kernel_path != str(path)
        or mount is None
        or mount.root is None
        or not _within(path, mount.point)
        or mount.device != (os.major(info.st_dev), os.minor(info.st_dev))
    ):
        raise PrivateMountError("private authority mount provenance does not match its pin")
    return _Coordinate(
        mount, mount.root / path.relative_to(mount.point), (info.st_dev, info.st_ino)
    )


@dataclass(frozen=True)
class _PrivatePath:
    path: Path
    anchor: Path
    fd: int
    suffix: tuple[str, ...]


class PrivateMountGuard:
    """Own private validation descriptors separately from inherited sources."""

    def __init__(self) -> None:
        self._pins = MountPins()
        self._private: list[_PrivatePath] = []
        self._sources: dict[Path, tuple[int, int]] = {}
        self._evidence: tuple[_Coordinate, ...] | None = None

    def __enter__(self) -> PrivateMountGuard:
        self._pins.__enter__()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._pins.close()

    def _pin_private(self, path: Path) -> _PrivatePath:
        anchor = path
        while True:
            try:
                fd = self._pins.open(anchor)
                break
            except MountPinError as exc:
                cause = exc.__cause__
                if (
                    not isinstance(cause, OSError)
                    or cause.errno != errno.ENOENT
                    or anchor == Path("/")
                ):
                    raise PrivateMountError("private authority path cannot be pinned") from exc
                anchor = anchor.parent
        suffix = path.relative_to(anchor).parts
        if suffix and not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise PrivateMountError("private authority missing-path anchor is not a directory")
        return _PrivatePath(path, anchor, fd, suffix)

    @staticmethod
    def _missing_unchanged(private: _PrivatePath) -> None:
        if not private.suffix:
            return
        try:
            os.stat(private.suffix[0], dir_fd=private.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PrivateMountError("private authority missing path cannot be rechecked") from exc
        raise PrivateMountError("private authority missing path changed during validation")

    def check(
        self,
        source_fds: Mapping[Path, int],
        mount_ids: Mapping[Path, int],
        private_paths: Sequence[Path],
    ) -> None:
        if (
            self._evidence is not None
            or not private_paths
            or len(private_paths) > _MAX_PRIVATE_PATHS
        ):
            raise PrivateMountError("private authority validation scope is invalid")
        self._sources = {path: (fd, mount_ids[path]) for path, fd in source_fds.items()}
        self._private = [self._pin_private(path) for path in private_paths]
        self._evidence = self._inspect()

    def _inspect(self) -> tuple[_Coordinate, ...]:
        table = _mount_table()
        sources = [
            _coordinate(path, fd, identity, table) for path, (fd, identity) in self._sources.items()
        ]
        authorities: list[_Coordinate] = []
        for private in self._private:
            self._missing_unchanged(private)
            coordinate = _coordinate(
                private.anchor, private.fd, self._pins.mount_id(private.fd), table
            )
            authorities.append(
                _Coordinate(
                    coordinate.mount, coordinate.path.joinpath(*private.suffix), coordinate.inode
                )
            )
            # A private child filesystem can also be exposed by an independent
            # bind. The initial boundary refuses this layout rather than omit it.
            if any(
                m.point != private.path and _within(m.point, private.path) for m in table.values()
            ):
                raise PrivateMountError("private authority contains an unsupported nested mount")
        for path in self._sources:
            if any(m.point != path and _within(m.point, path) for m in table.values()):
                raise PrivateMountError("private authority source topology changed")
        for source in sources:
            for private, authority in zip(self._private, authorities, strict=True):
                same_inode = not private.suffix and source.inode == authority.inode
                same_tree = source.mount.device == authority.mount.device and (
                    _within(source.path, authority.path) or _within(authority.path, source.path)
                )
                if same_inode or same_tree:
                    raise PrivateMountError("mount overlaps private authority")
        return tuple(sources + authorities)

    def recheck(self) -> None:
        self._pins.recheck()
        if self._evidence is None or self._inspect() != self._evidence:
            raise PrivateMountError("private authority mount provenance changed during validation")
