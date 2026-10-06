"""Provider-neutral pinned Linux process namespace; never an approval authority.

The caller owns invocation and cleanup, inherits only SandboxLaunch.pass_fds
with close_fds=True, and never retries an unsuccessful launch without isolation.
Read-only project access and private networking are the defaults. This builder
pins mount identities; it does not freeze contents or establish host-wide custody.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .claude_mount_pins import MountPinError, MountPins, SandboxLaunch, mount_id


class NamespaceError(ValueError):
    """A confined process cannot safely start."""


_SANDBOX_HOME = "/home/example"
_SANDBOX_SOCKET = "/run/hub-permission.sock"
_SYSTEM_ALIASES = {"/bin": "/usr/bin", "/lib": "/usr/lib", "/lib64": "/usr/lib64"}
_MAX_RUNTIME_ENTRIES = 100_000
_MAX_WRITABLE_ENTRIES = 100_000
_MAX_SCAN_DEPTH = 128
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
        raise NamespaceError(f"{label} must be an absolute canonical path")
    # Reject symlinked components: resolving them silently could move a mount
    # source across the private/project boundary after it was checked.
    cursor = Path("/")
    for part in path.parts[1:]:
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            if exists:
                raise NamespaceError(f"{label} does not exist") from None
            continue
        if stat.S_ISLNK(mode):
            raise NamespaceError(f"{label} has a symlinked component")
    if exists and not path.exists():
        raise NamespaceError(f"{label} does not exist")
    return path


def _not_broad(path: Path, label: str) -> None:
    # A source mounted at / or a generic operator directory can expose much
    # more than the requested file-tool surface even when bound read-only.
    if (
        len(path.parts) < 3
        or path in (Path("/home"), Path("/tmp"), Path("/run"), Path("/root"))
        or path.parent == Path("/home")
    ):
        raise NamespaceError(f"{label} is too broad")


def _runtime_location(path: Path) -> None:
    if any(_within(path, prefix) for prefix in _RUNTIME_PREFIXES):
        return
    if _within(path, Path("/opt")) and path != Path("/opt"):
        return
    raise NamespaceError("trusted code mount is outside runtime locations")


def _immutable_source(path: Path, label: str, *, directory: bool | None = None) -> None:
    if directory is True and not path.is_dir():
        raise NamespaceError(f"{label} must be a directory")
    if directory is False and not path.is_file():
        raise NamespaceError(f"{label} must be a regular file")
    for parent in (path, *path.parents):
        _immutable_entry(parent, label, parent.stat())


def _immutable_entry(path: Path, label: str, info: os.stat_result) -> None:
    if info.st_uid != 0 or info.st_mode & 0o022 or os.access(path, os.W_OK, effective_ids=True):
        raise NamespaceError(f"{label} must be immutable root-owned code")
    try:
        if {"system.posix_acl_access", "system.posix_acl_default"}.intersection(
            os.listxattr(path, follow_symlinks=False)
        ):
            raise NamespaceError(f"{label} has unsupported ACL permissions")
    except OSError as exc:
        if exc.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise NamespaceError("cannot inspect runtime ACLs") from exc


def _immutable_tree(path: Path) -> None:
    """Reject writable descendants of a readonly runtime/code mount.

    A readonly namespace bind does not make host files immutable to the same
    UID via another process. Limit the walk to reject accidentally broad roots.
    """
    if not path.is_dir():
        return
    _immutable_source(path, "runtime root", directory=True)
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
        _immutable_entry(directory, "runtime directory", info)
        for entry in directory.iterdir():
            count += 1
            if count > _MAX_RUNTIME_ENTRIES:
                raise NamespaceError("runtime root exceeds trust scan limit")
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = entry.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise NamespaceError("runtime tree contains an unsafe symlink") from None
                _immutable_source(target, "runtime symlink target")
                if target.is_dir():
                    pending.append(target)
            elif stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode):
                _immutable_entry(entry, "runtime entry", info)
            else:
                raise NamespaceError("runtime tree contains a special entry")


def _scan_writable_tree(root: Path, source_fd: int) -> set[tuple[int, int]]:
    """Scan the pinned source, never a replacement bearing the same path name."""
    try:
        return _scan_pinned_tree(source_fd)
    except (OSError, MountPinError) as exc:
        error = exc if isinstance(exc, OSError) else exc.__cause__
        if not isinstance(error, OSError):
            raise
        if error.errno in {errno.ENOENT, errno.ESTALE}:
            message = "writable tree changed during validation"
        elif error.errno in {errno.EMFILE, errno.ENFILE}:
            message = "writable tree descriptor limit exceeded"
        else:
            message = "cannot inspect writable tree"
        raise NamespaceError(message) from exc


def _scan_pinned_tree(source_fd: int) -> set[tuple[int, int]]:
    """Own only the current ancestor chain while examining each pinned entry."""
    count = 0
    expected_mount = mount_id(source_fd)
    root_info = os.fstat(source_fd)
    expected_device = root_info.st_dev
    identities = {(root_info.st_dev, root_info.st_ino)}
    first = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=source_fd)
    try:
        entries = os.scandir(first)
    except BaseException:
        os.close(first)
        raise
    frames = [(first, entries)]
    try:
        while frames:
            current, entries = frames[-1]
            entry = next(entries, None)
            if entry is None:
                frames.pop()
                entries.close()
                os.close(current)
                continue
            count += 1
            if count > _MAX_WRITABLE_ENTRIES:
                raise NamespaceError("writable tree exceeds trust scan limit")
            fd = os.open(entry.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            try:
                info = os.fstat(fd)
                if mount_id(fd) != expected_mount:
                    raise NamespaceError("writable tree contains a nested mount")
                if info.st_dev != expected_device:
                    raise NamespaceError("writable tree contains a nested filesystem")
                if info.st_ino != entry.inode():
                    raise NamespaceError("writable tree changed during validation")
                if stat.S_ISLNK(info.st_mode):
                    continue  # Outside targets stay absent in the new namespace.
                if stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise NamespaceError("writable tree contains a hardlink")
                elif stat.S_ISDIR(info.st_mode):
                    if len(frames) >= _MAX_SCAN_DEPTH:
                        raise NamespaceError("writable tree exceeds scan depth limit")
                    child = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=fd)
                    try:
                        child_entries = os.scandir(child)
                    except BaseException:
                        os.close(child)
                        raise
                    frames.append((child, child_entries))
                else:
                    raise NamespaceError("writable tree contains a special file")
                identities.add((info.st_dev, info.st_ino))
            finally:
                os.close(fd)
    finally:
        for descriptor, entries in reversed(frames):
            entries.close()
            os.close(descriptor)
    return identities


def _reject_nested_mounts(*roots: Path, mount_ids: Mapping[Path, int] | None = None) -> None:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise NamespaceError("cannot inspect host mount table") from exc
    filesystems: list[tuple[int, Path, str]] = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if len(fields) < 5 or not fields[0].isdigit() or not separator or not after.split():
            raise NamespaceError("host mount table is malformed")
        mountpoint = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
        filesystems.append((int(fields[0]), mountpoint, after.split()[0]))
        for root in roots:
            if mountpoint != root and _within(mountpoint, root):
                raise NamespaceError("writable tree contains a nested mount")
    for root in roots:
        if mount_ids is not None:
            selected = [item for item in filesystems if item[0] == mount_ids[root]]
        else:
            candidates = [item for item in filesystems if _within(root, item[1])]
            depth = max((len(item[1].parts) for item in candidates), default=0)
            selected = [item for item in candidates if len(item[1].parts) == depth]
        if (
            len(selected) != 1
            or not _within(root, selected[0][1])
            or selected[0][2]
            not in {
                "ext4",
                "xfs",
                "btrfs",
                "tmpfs",
            }
        ):
            raise NamespaceError("writable roots require a supported native Linux filesystem")


def _validate_destinations(project: Path, roots: tuple[Path, ...], code_root: Path | None) -> None:
    """Reject ordinary mounts shadowing namespace-owned destinations.

    HOME is remapped, and the optional socket has one fixed /run destination.
    Deliberate readonly runtime nesting and the .git overlay remain valid.
    """
    destinations = (project, *roots, *((code_root,) if code_root is not None else ()))
    for destination in destinations:
        if destination == Path("/tmp") or any(
            _within(destination, reserved) or _within(reserved, destination)
            for reserved in map(Path, (_SANDBOX_HOME, "/proc", "/dev", "/run"))
        ):
            raise NamespaceError("mount destination overlaps a namespace-owned path")
    for alias in map(Path, _SYSTEM_ALIASES):
        if _within(project, alias) or _within(alias, project):
            raise NamespaceError("project destination overlaps a system alias")


@dataclass(frozen=True)
class NamespaceRuntime:
    """Capture the immutable runtime baseline once, rather than at each launch."""

    bwrap_executable: Path
    executable: Path
    runtime_roots: tuple[Path, ...]
    auxiliary_executables: tuple[Path, ...] = ()
    code_root: Path | None = None
    _pins: tuple[tuple[int, int, int, int], ...] = field(init=False, repr=False)

    def _sources(self) -> tuple[Path, ...]:
        return (
            self.bwrap_executable,
            self.executable,
            *self.auxiliary_executables,
            *((self.code_root,) if self.code_root is not None else ()),
            *self.runtime_roots,
        )

    def __post_init__(self) -> None:
        for source in self._sources():
            _absolute_path(source, "trusted runtime source")
        object.__setattr__(
            self, "_pins", tuple(self._runtime_identity(Path(p)) for p in self._sources())
        )
        for root in self.runtime_roots:
            _immutable_tree(Path(root))
        if self.code_root is not None and not any(
            _within(Path(self.code_root), Path(root)) for root in self.runtime_roots
        ):
            _immutable_tree(Path(self.code_root))

    @staticmethod
    def _runtime_identity(path: Path) -> tuple[int, int, int, int]:
        try:
            info = path.stat()
        except OSError as exc:
            raise NamespaceError("trusted runtime source is unavailable") from exc
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def validate(self) -> tuple[Path, tuple[Path, ...]]:
        bwrap = _absolute_path(self.bwrap_executable, "bwrap executable")
        roots = tuple(_absolute_path(p, "runtime root") for p in self.runtime_roots)
        if not roots or len(set(roots)) != len(roots):
            raise NamespaceError("runtime roots must be explicit and unique")
        for root in roots:
            _not_broad(root, "runtime root")
            _runtime_location(root)
            _immutable_source(root, "runtime root")
        _immutable_source(bwrap, "bwrap executable", directory=False)
        if self.code_root is not None:
            code = _absolute_path(self.code_root, "trusted code root")
            _not_broad(code, "trusted code root")
            _runtime_location(code)
            _immutable_source(code, "trusted code root", directory=True)
        if tuple(self._runtime_identity(Path(p)) for p in self._sources()) != self._pins:
            raise NamespaceError("trusted runtime identity changed")
        for path in (self.executable, *self.auxiliary_executables):
            executable = _absolute_path(path, "runtime executable")
            _immutable_source(executable, "runtime executable", directory=False)
            if not any(_within(executable, root) for root in roots):
                raise NamespaceError("runtime executable is outside readonly runtime mounts")
        return bwrap, roots


@dataclass(frozen=True)
class ProcessNamespaceConfig:
    """Explicit sources and access modes for one confined process.

    The separate 0700 session_home is writable at a fixed destination. Private
    authority paths are absent. Only an explicitly supplied per-turn permission
    socket may be exposed at its existing fixed destination.
    """

    runtime: NamespaceRuntime
    project_root: Path
    session_home: Path
    private_paths: tuple[Path, ...]
    project_access: str = "read-only"
    network: str = "private"
    permission_socket: Path | None = None

    def __post_init__(self) -> None:
        if self.project_access not in ("read-only", "read-write"):
            raise NamespaceError("unsupported project access mode")
        if self.network not in ("private", "shared"):
            raise NamespaceError("unsupported network mode")
        if self.network == "private" and self.permission_socket is not None:
            raise NamespaceError("private network profile cannot expose a permission socket")

    def _validate(
        self, mount_ids: Mapping[Path, int], source_fds: Mapping[Path, int]
    ) -> tuple[Path, Path, Path, tuple[Path, ...], Path | None]:
        if sys.platform != "linux":
            raise NamespaceError("Linux namespaces are required")
        project = _absolute_path(self.project_root, "project root")
        home = _absolute_path(self.session_home, "session home")
        if project == home:
            raise NamespaceError("project and session-home mount roles overlap")
        _not_broad(project, "project root")
        _not_broad(home, "session home")
        if not all(stat.S_ISDIR(os.fstat(source_fds[path]).st_mode) for path in (project, home)):
            raise NamespaceError("project and session home must be directories")
        git = project / ".git"
        if not stat.S_ISDIR(os.fstat(source_fds[git]).st_mode):
            raise NamespaceError("project must have an ordinary .git directory")
        home_stat = os.fstat(source_fds[home])
        home_identity = (home_stat.st_dev, home_stat.st_ino)
        for protected in (project, git):
            protected_stat = os.fstat(source_fds[protected])
            if home_identity == (protected_stat.st_dev, protected_stat.st_ino):
                raise NamespaceError("protected project and session-home mount roles overlap")
        if home_stat.st_uid != os.geteuid() or stat.S_IMODE(home_stat.st_mode) != 0o700:
            raise NamespaceError("session home must be owned by worker UID and mode 0700")
        _reject_nested_mounts(*source_fds, mount_ids=mount_ids)
        project_ids = _scan_writable_tree(project, source_fds[project])
        home_ids = _scan_writable_tree(home, source_fds[home])
        if not project_ids.isdisjoint(home_ids):
            raise NamespaceError("project and session-home tree mount roles overlap")
        sock = None
        if self.permission_socket is not None:
            sock = _absolute_path(self.permission_socket, "permission socket")
            socket_stat = os.fstat(source_fds[sock])
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise NamespaceError("permission endpoint must be a Unix socket")
            parent_stat = sock.parent.stat()
            if (
                socket_stat.st_uid != os.geteuid()
                or stat.S_IMODE(socket_stat.st_mode) != 0o600
                or parent_stat.st_uid != os.geteuid()
                or stat.S_IMODE(parent_stat.st_mode) != 0o700
            ):
                raise NamespaceError("permission socket and parent need private ownership and mode")
        bwrap, roots = self.runtime.validate()
        code = self.runtime.code_root
        sources = (project, home, *roots, *((code,) if code is not None else ()))
        for writable in (project, home):
            for other in sources:
                if writable != other and (_within(writable, other) or _within(other, writable)):
                    raise NamespaceError("writable and other mounts overlap")
        if not self.private_paths:
            raise NamespaceError("private authority paths must be explicit")
        for raw in self.private_paths:
            private = _absolute_path(raw, "private authority path", exists=False)
            _not_broad(private, "private authority path")
            if any(_within(source, private) or _within(private, source) for source in sources):
                raise NamespaceError("mount overlaps private authority")
        if sock is not None and any(
            _within(source, sock) or _within(sock, source) for source in sources
        ):
            raise NamespaceError("permission socket overlaps a broad mount")
        _validate_destinations(project, roots, code)
        return bwrap, project, home, roots, sock

    def wrap(self, argv: Sequence[str], env: Mapping[str, str], cwd: Path | str) -> SandboxLaunch:
        """Pin, validate and transfer descriptor ownership to the existing caller."""
        pins = MountPins()
        try:
            command, child = self._build(argv, env, cwd, pins)
            pins.recheck()
            return SandboxLaunch(tuple(command), child, pins)
        except BaseException as exc:
            pins.close()
            if isinstance(exc, (MountPinError, OSError)):
                raise NamespaceError("mount sources could not be pinned") from exc
            raise

    def _build(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: Path | str, pins: MountPins
    ) -> tuple[list[str], dict[str, str]]:
        project_fd = pins.open(Path(self.project_root), directory=True)
        git_fd = pins.open_relative(project_fd, ".git", directory=True)
        home_fd = pins.open(Path(self.session_home), directory=True)
        sock_fd = (
            pins.open(Path(self.permission_socket)) if self.permission_socket is not None else None
        )
        runtime_fds = {Path(root): pins.open(Path(root)) for root in self.runtime.runtime_roots}
        code = self.runtime.code_root
        code_fd = None
        if code is not None and not any(_within(code, root) for root in runtime_fds):
            code_fd = pins.open(code, directory=True)
        source_fds = {
            Path(self.project_root): project_fd,
            Path(self.project_root) / ".git": git_fd,
            Path(self.session_home): home_fd,
            **runtime_fds,
        }
        if self.permission_socket is not None and sock_fd is not None:
            source_fds[Path(self.permission_socket)] = sock_fd
        if code is not None and code_fd is not None:
            source_fds[code] = code_fd
        bwrap, project, home, roots, sock = self._validate(
            {path: pins.mount_id(fd) for path, fd in source_fds.items()}, source_fds
        )
        _require_fd_bind_support(bwrap)
        workdir = _absolute_path(cwd, "working directory")
        if not _within(workdir, project) or not workdir.is_dir():
            raise NamespaceError("working directory is outside project")
        if not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise NamespaceError("invalid provider argv")
        executable = _absolute_path(argv[0], "provider executable")
        if executable != self.runtime.executable:
            raise NamespaceError("provider executable differs from pinned runtime executable")
        if any(
            not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v
            for k, v in env.items()
        ):
            raise NamespaceError("invalid provider environment")
        child_env = dict(env)
        child_env.update(
            HOME=_SANDBOX_HOME,
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
        ]
        if self.network == "shared":
            command.append("--share-net")
        command.extend(
            (
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
            )
        )
        for root in roots:
            command.extend(("--ro-bind-fd", str(runtime_fds[root]), str(root)))
        for alias, target in _SYSTEM_ALIASES.items():
            alias_path = Path(alias)
            if (
                alias_path.is_symlink()
                and alias_path.resolve() == Path(target)
                and any(_within(root, Path(target)) for root in roots)
            ):
                command.extend(("--symlink", target.lstrip("/"), alias))
        if code is not None and code_fd is not None:
            command.extend(("--ro-bind-fd", str(code_fd), str(code)))
        project_flag = "--ro-bind-fd" if self.project_access == "read-only" else "--bind-fd"
        command.extend(
            (
                project_flag,
                str(project_fd),
                str(project),
                "--ro-bind-fd",
                str(git_fd),
                str(project / ".git"),
                "--bind-fd",
                str(home_fd),
                _SANDBOX_HOME,
            )
        )
        if sock is not None and sock_fd is not None:
            command.extend(("--ro-bind-fd", str(sock_fd), _SANDBOX_SOCKET))
        command.extend(("--chdir", str(workdir), "--", *argv))
        return command, child_env


def _require_fd_bind_support(bwrap: Path) -> None:
    """Passive immutable-runtime check; no provider or namespace is started."""
    try:
        result = subprocess.run(
            [str(bwrap), "--help"],
            cwd="/",
            env={"LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=3,
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NamespaceError("bubblewrap descriptor binds could not be verified") from exc
    if (
        result.returncode
        or len(result.stdout) > 131072
        or any(
            not re.search(rb"(?m)^\s*" + flag + rb"\s+FD\s+DEST\b", result.stdout)
            for flag in (b"--bind-fd", b"--ro-bind-fd")
        )
    ):
        raise NamespaceError("bubblewrap descriptor binds are required")
