"""Guarded Linux mount namespace for a narrow Claude file-tool turn.

This module builds an OS boundary, not a Claude permission decision. The caller
must supply an already validated registry root, exact Claude tool/permission
policy, and an approval host. Launch the returned argv/env with close_fds=True,
pass_fds=(), and never retry a failed bwrap launch without the sandbox.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence


class FileToolSandboxError(ValueError):
    """A file-tool turn cannot safely start."""


_SAFE_ENV = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
    }
)
_SANDBOX_HOME = "/home/example"
_SANDBOX_SOCKET = "/run/hub-permission.sock"
_SYSTEM_ALIASES = {"/bin": "/usr/bin", "/lib": "/usr/lib", "/lib64": "/usr/lib64"}
_MAX_RUNTIME_ENTRIES = 100_000
_MAX_WRITABLE_ENTRIES = 100_000
_RUNTIME_PREFIXES = (
    Path("/usr/bin"),
    Path("/usr/lib"),
    Path("/usr/lib64"),
    Path("/usr/local/bin"),
    Path("/usr/local/lib"),
    Path("/usr/share"),
)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _absolute_path(raw: Path | str, label: str, *, exists: bool = True) -> Path:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise FileToolSandboxError(f"{label} must be an absolute canonical path")
    # Reject symlinked components: resolving them silently could move a mount
    # source across the private/project boundary after it was checked.
    cursor = Path("/")
    for part in path.parts[1:]:
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            if exists:
                raise FileToolSandboxError(f"{label} does not exist") from None
            continue
        if stat.S_ISLNK(mode):
            raise FileToolSandboxError(f"{label} has a symlinked component")
    if exists and not path.exists():
        raise FileToolSandboxError(f"{label} does not exist")
    return path


def _not_broad(path: Path, label: str) -> None:
    # A source mounted at / or a generic operator directory can expose much
    # more than the requested file-tool surface even when bound read-only.
    if (
        len(path.parts) < 3
        or path in (Path("/home"), Path("/tmp"), Path("/run"), Path("/root"))
        or path.parent == Path("/home")
    ):
        raise FileToolSandboxError(f"{label} is too broad")


def _runtime_location(path: Path) -> None:
    if any(_within(path, prefix) for prefix in _RUNTIME_PREFIXES):
        return
    if _within(path, Path("/opt")) and path != Path("/opt"):
        return
    raise FileToolSandboxError("trusted code mount is outside runtime locations")


def _immutable_source(path: Path, label: str, *, directory: bool | None = None) -> None:
    if directory is True and not path.is_dir():
        raise FileToolSandboxError(f"{label} must be a directory")
    if directory is False and not path.is_file():
        raise FileToolSandboxError(f"{label} must be a regular file")
    for parent in (path, *path.parents):
        info = parent.stat()
        if (
            info.st_uid == os.getuid()
            or info.st_mode & 0o002
            or (info.st_gid in {*os.getgroups(), os.getegid()} and info.st_mode & 0o020)
            or os.access(parent, os.W_OK, effective_ids=True)
        ):
            raise FileToolSandboxError(f"{label} is writable by the provider UID")


def _immutable_tree(path: Path) -> None:
    """Reject writable descendants of a readonly runtime/code mount.

    A readonly namespace bind does not make host files immutable to the same
    UID via another process. Limit the walk to reject accidentally broad roots.
    """
    if not path.is_dir():
        return
    count = 0
    pending = [path]
    visited: set[tuple[int, int]] = set()
    while pending:
        directory = pending.pop()
        info = directory.stat()
        identity = (info.st_dev, info.st_ino)
        if identity in visited:
            continue
        visited.add(identity)
        _immutable_source(directory, "runtime directory", directory=True)
        for entry in directory.iterdir():
            count += 1
            if count > _MAX_RUNTIME_ENTRIES:
                raise FileToolSandboxError("runtime root exceeds trust scan limit")
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = entry.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise FileToolSandboxError("runtime tree contains an unsafe symlink") from None
                _immutable_source(target, "runtime symlink target")
                if target.is_dir():
                    pending.append(target)
            elif stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode):
                if (
                    info.st_uid == os.getuid()
                    or info.st_mode & 0o002
                    or (info.st_gid in {*os.getgroups(), os.getegid()} and info.st_mode & 0o020)
                    or os.access(entry, os.W_OK, effective_ids=True)
                ):
                    raise FileToolSandboxError("runtime tree contains a provider-writable entry")
            else:
                raise FileToolSandboxError("runtime tree contains a special entry")


def _scan_writable_tree(root: Path) -> None:
    """Refuse hardlinks, special files, and mounted subtrees in writable binds."""
    count = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            count += 1
            if count > _MAX_WRITABLE_ENTRIES:
                raise FileToolSandboxError("writable tree exceeds trust scan limit")
            info = (Path(base) / name).lstat()
            if stat.S_ISLNK(info.st_mode):
                continue  # Outside targets remain absent in the new namespace.
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise FileToolSandboxError("writable tree contains a hardlink")
            elif not stat.S_ISDIR(info.st_mode):
                raise FileToolSandboxError("writable tree contains a special file")


def _reject_nested_mounts(*roots: Path) -> None:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise FileToolSandboxError("cannot inspect host mount table") from exc
    filesystems: list[tuple[Path, str]] = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if len(fields) < 5 or not separator or not after.split():
            raise FileToolSandboxError("host mount table is malformed")
        mountpoint = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
        filesystems.append((mountpoint, after.split()[0]))
        for root in roots:
            if mountpoint != root and _within(mountpoint, root):
                raise FileToolSandboxError("writable tree contains a nested mount")
    for root in roots:
        selected = [item for item in filesystems if _within(root, item[0])]
        if not selected or max(selected, key=lambda item: len(item[0].parts))[1] not in {
            "ext4",
            "xfs",
            "btrfs",
            "tmpfs",
        }:
            raise FileToolSandboxError("writable roots require a supported native Linux filesystem")


@dataclass(frozen=True)
class FileToolSandboxConfig:
    """Explicit sources for one provider turn; no implicit host mounts.

    Runtime roots and hook code must be installed by a different UID and not
    writable by the provider UID or its groups. ``provider_home`` is the one
    dedicated 0700 session store and must contain no Hub/tlive authority.
    ``private_paths`` must enumerate those authority roots/files; the sole
    exposed socket is an explicit per-turn exception, mounted as one inode.
    """

    bwrap_executable: Path
    project_root: Path
    provider_home: Path
    runtime_roots: tuple[Path, ...]
    claude_executable: Path
    python_executable: Path
    hook_code_root: Path
    permission_socket: Path
    private_paths: tuple[Path, ...]
    _pins: tuple[tuple[int, int, int, int], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        sources = (
            self.bwrap_executable,
            self.claude_executable,
            self.python_executable,
            self.hook_code_root,
            *self.runtime_roots,
        )
        for source in sources:
            _absolute_path(source, "trusted runtime source")
        object.__setattr__(self, "_pins", tuple(self._identity(Path(p)) for p in sources))
        for root in self.runtime_roots:
            _immutable_tree(Path(root))
        if not any(_within(Path(self.hook_code_root), Path(root)) for root in self.runtime_roots):
            _immutable_tree(Path(self.hook_code_root))

    @staticmethod
    def _identity(path: Path) -> tuple[int, int, int, int]:
        try:
            info = path.stat()
        except OSError as exc:
            raise FileToolSandboxError("trusted runtime source is unavailable") from exc
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _validate(self) -> tuple[Path, Path, Path, tuple[Path, ...], Path]:
        if sys.platform != "linux":
            raise FileToolSandboxError("Linux namespaces are required")
        bwrap = _absolute_path(self.bwrap_executable, "bwrap executable")
        project = _absolute_path(self.project_root, "project root")
        home = _absolute_path(self.provider_home, "provider home")
        hook = _absolute_path(self.hook_code_root, "hook code root")
        sock = _absolute_path(self.permission_socket, "permission socket")
        _not_broad(project, "project root")
        _not_broad(home, "provider home")
        _not_broad(hook, "hook code root")
        _runtime_location(hook)
        if not project.is_dir() or not home.is_dir():
            raise FileToolSandboxError("project and provider home must be directories")
        git = project / ".git"
        if git.is_symlink() or not git.is_dir():
            raise FileToolSandboxError("project must have an ordinary .git directory")
        if home.stat().st_uid != os.getuid() or stat.S_IMODE(home.stat().st_mode) != 0o700:
            raise FileToolSandboxError("provider home must be owned by worker UID and mode 0700")
        _reject_nested_mounts(project, home)
        _scan_writable_tree(project)
        _scan_writable_tree(home)
        if not stat.S_ISSOCK(sock.stat().st_mode):
            raise FileToolSandboxError("permission endpoint must be a Unix socket")
        socket_stat = sock.stat()
        parent_stat = sock.parent.stat()
        if (
            socket_stat.st_uid != os.getuid()
            or stat.S_IMODE(socket_stat.st_mode) != 0o600
            or parent_stat.st_uid != os.getuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise FileToolSandboxError(
                "permission socket and parent need private ownership and mode"
            )

        roots = tuple(_absolute_path(p, "runtime root") for p in self.runtime_roots)
        if not roots or len(set(roots)) != len(roots):
            raise FileToolSandboxError("runtime roots must be explicit and unique")
        for root in roots:
            _not_broad(root, "runtime root")
            _runtime_location(root)
            _immutable_source(root, "runtime root")
        _immutable_source(bwrap, "bwrap executable", directory=False)
        _immutable_source(hook, "hook code root", directory=True)
        pinned_sources = (
            bwrap,
            self.claude_executable,
            self.python_executable,
            hook,
            *roots,
        )
        if tuple(self._identity(Path(p)) for p in pinned_sources) != self._pins:
            raise FileToolSandboxError("trusted runtime identity changed")
        for name, path in (
            ("Claude executable", self.claude_executable),
            ("Python executable", self.python_executable),
        ):
            executable = _absolute_path(path, name)
            _immutable_source(executable, name, directory=False)
            if not any(_within(executable, root) for root in roots):
                raise FileToolSandboxError(f"{name} is outside readonly runtime mounts")
        sources = (project, home, hook, *roots)
        for writable in (project, home):
            for other in sources:
                if writable != other and (_within(writable, other) or _within(other, writable)):
                    raise FileToolSandboxError("writable and other mounts overlap")
        if not self.private_paths:
            raise FileToolSandboxError("private authority paths must be explicit")
        for raw in self.private_paths:
            private = _absolute_path(raw, "private authority path", exists=False)
            _not_broad(private, "private authority path")
            if any(_within(source, private) or _within(private, source) for source in sources):
                raise FileToolSandboxError("mount overlaps private authority")
        if any(_within(source, sock) or _within(sock, source) for source in sources):
            raise FileToolSandboxError("permission socket overlaps a broad mount")
        return bwrap, project, home, roots, sock

    def wrap(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: Path | str
    ) -> tuple[list[str], dict[str, str]]:
        """Build an argv-only bwrap launch and sanitized child environment.

        A bwrap startup/namespace failure is a failed turn. The caller must
        never invoke ``argv`` alone or retry outside this wrapper.
        """
        bwrap, project, home, roots, sock = self._validate()
        workdir = _absolute_path(cwd, "working directory")
        if not _within(workdir, project) or not workdir.is_dir():
            raise FileToolSandboxError("working directory is outside project")
        if not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise FileToolSandboxError("invalid provider argv")
        executable = _absolute_path(argv[0], "provider executable")
        if executable != self.claude_executable:
            raise FileToolSandboxError("provider executable differs from pinned Claude executable")
        if any(
            not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v
            for k, v in env.items()
        ):
            raise FileToolSandboxError("invalid provider environment")
        unknown = set(env) - _SAFE_ENV
        if unknown:
            raise FileToolSandboxError("provider environment includes unapproved names")
        child_env = dict(env)
        child_env.update(
            HOME=_SANDBOX_HOME,
            CLAUDE_CONFIG_DIR=f"{_SANDBOX_HOME}/.claude",
            XDG_CONFIG_HOME=f"{_SANDBOX_HOME}/.config",
            XDG_CACHE_HOME=f"{_SANDBOX_HOME}/.cache",
            XDG_STATE_HOME=f"{_SANDBOX_HOME}/.local/state",
            XDG_DATA_HOME=f"{_SANDBOX_HOME}/.local/share",
            TMPDIR="/tmp",
            PATH="/usr/bin:/bin",
        )
        command = [
            str(bwrap),
            "--unshare-user",
            "--unshare-all",
            "--unshare-pid",
            "--share-net",  # CPA loopback; no host filesystem or PID namespace.
            "--disable-userns",
            "--assert-userns-disabled",
            "--cap-drop",
            "ALL",
            "--new-session",
            "--die-with-parent",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--remount-ro",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/run",
            "--dir",
            "/home",
        ]
        for root in roots:
            command.extend(("--ro-bind", str(root), str(root)))
        for alias, target in _SYSTEM_ALIASES.items():
            alias_path = Path(alias)
            if (
                alias_path.is_symlink()
                and alias_path.resolve() == Path(target)
                and any(_within(root, Path(target)) for root in roots)
            ):
                command.extend(("--symlink", target.lstrip("/"), alias))
        hook = Path(self.hook_code_root)
        if not any(_within(hook, root) for root in roots):
            command.extend(("--ro-bind", str(hook), str(hook)))
        command.extend(
            (
                "--bind",
                str(project),
                str(project),
                "--ro-bind",
                str(project / ".git"),
                str(project / ".git"),
                "--bind",
                str(home),
                _SANDBOX_HOME,
                "--ro-bind",
                str(sock),
                _SANDBOX_SOCKET,
                "--chdir",
                str(workdir),
                "--",
                *argv,
            )
        )
        return command, child_env
