"""Pin only the metadata needed by one ordinary or linked Git worktree.

Linked roots share objects/refs, never the primary checkout or other worktree
indices. The synthetic common directory intentionally cannot run repository
administration such as pack-refs, worktree creation or configuration changes.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .claude_mount_pins import MountPinError, MountPins


def _control(fd: int) -> str:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192:
        raise MountPinError("invalid Git control file")
    with open(f"/proc/self/fd/{fd}", "rb") as stream:
        raw = stream.read(8193)
    try:
        text = raw.decode("utf-8").removesuffix("\n")
    except UnicodeError:
        raise MountPinError("invalid Git control encoding") from None
    if not text or len(raw) > 8192 or any(c in text for c in "\x00\r\n"):
        raise MountPinError("invalid Git control content")
    return text


def _target(parent: Path, value: str) -> Path:
    path = Path(value)
    return Path(os.path.normpath(path if path.is_absolute() else parent / path))


@dataclass(frozen=True)
class GitSources:
    descriptors: dict[Path, int]
    readonly: frozenset[Path]
    external_directories: tuple[Path, ...] = ()
    controls: tuple[tuple[int, str], ...] = ()

    def recheck(self) -> None:
        for fd, expected in self.controls:
            if _control(fd) != expected:
                raise MountPinError("Git control binding changed")

    def arguments(self, access: str) -> list[str]:
        args: list[str] = []
        for path, fd in self.descriptors.items():
            flag = "--ro-bind-fd" if access == "read-only" or path in self.readonly else "--bind-fd"
            args.extend((flag, str(fd), str(path)))
        return args


def pin_git_sources(pins: MountPins, project_fd: int, project: Path) -> GitSources:
    gitfile = project / ".git"
    fd = pins.open_relative(project_fd, ".git")
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        return GitSources({gitfile: fd}, frozenset())
    pointer = _control(fd)
    if not pointer.startswith("gitdir: "):
        raise MountPinError("unsupported Git control file")
    gitdir = _target(project, pointer.removeprefix("gitdir: "))
    if gitdir.parent.name != "worktrees" or gitdir.name in ("", ".", ".."):
        raise MountPinError("unsupported linked Git layout")
    common = gitdir.parent.parent
    # The common directory is NOT mounted. Only selected entries below it are.
    git_fd = pins.open(gitdir, directory=True)
    back_fd = pins.open_relative(git_fd, "gitdir", directory=False)
    common_fd = pins.open_relative(git_fd, "commondir", directory=False)
    backlink, commonlink = _control(back_fd), _control(common_fd)
    if _target(gitdir, backlink) != gitfile or _target(gitdir, commonlink) != common:
        raise MountPinError("linked Git identity does not match project")
    descriptors = {
        gitfile: fd,
        gitdir: git_fd,
        gitdir / "gitdir": back_fd,
        gitdir / "commondir": common_fd,
    }
    readonly = {gitfile, gitdir / "gitdir", gitdir / "commondir"}
    directories = [gitdir]
    for relative in (
        "objects",
        "refs",
        "logs/refs",
        "config",
        "packed-refs",
        "shallow",
        "info",
        "hooks",
    ):
        path = common / relative
        if not path.exists() and not path.is_symlink():
            if relative in ("objects", "refs", "config"):
                raise MountPinError("linked Git metadata is missing")
            continue
        directory = relative in ("objects", "refs", "logs/refs", "info", "hooks")
        descriptors[path] = pins.open(path, directory=directory)
        if not directory and os.fstat(descriptors[path]).st_nlink != 1:
            raise MountPinError("Git metadata contains a hardlink")
        if directory:
            directories.append(path)
        if relative not in ("objects", "refs", "logs/refs"):
            readonly.add(path)
    return GitSources(
        descriptors,
        frozenset(readonly),
        tuple(directories),
        ((fd, pointer), (back_fd, backlink), (common_fd, commonlink)),
    )
