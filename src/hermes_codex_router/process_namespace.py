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
from .claude_private_mounts import PrivateMountError, PrivateMountGuard, read_mount_table
from .namespace_git import pin_git_sources
from .namespace_toolchain import NamespaceToolchain, git_environment, validate_control_interpreters


class NamespaceError(ValueError):
    """A confined process cannot safely start."""


class TrustScanLimitError(NamespaceError):
    """A tree has more entries than the trust scan will inspect."""


SANDBOX_HOME = "/home/example"
_SANDBOX_SOCKET = "/run/hub-permission.sock"
_SYSTEM_ALIASES = {"/bin": "/usr/bin", "/lib": "/usr/lib", "/lib64": "/usr/lib64"}
_SHELL_ALIAS = Path("/usr/bin/sh")
_SHELL_TARGET = Path("/usr/bin/dash")
_MAX_RUNTIME_ENTRIES = 100_000
_MAX_WRITABLE_ENTRIES = 100_000
_MAX_SCAN_DEPTH = 128
# A wide-view profile may show these whole system trees read-only. Everything
# else must be an explicit narrow tree; host state and sockets stay absent.
_SYSTEM_VISIBLE_ROOTS = frozenset(map(Path, ("/usr", "/etc", "/opt")))
_NEVER_VISIBLE = tuple(
    map(Path, ("/proc", "/dev", "/sys", "/run", "/tmp", "/var", "/mnt", "/root", "/boot"))
)
_MAX_VISIBLE_CHILDREN = 4096
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
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise NamespaceError(f"{label} contains an unsupported runtime hardlink")
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
                raise TrustScanLimitError("runtime root exceeds trust scan limit")
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


def _scan_writable_tree(
    root: Path, source_fd: int, *, toolchain: bool = False
) -> set[tuple[int, int]]:
    """Scan the pinned source, never a replacement bearing the same path name."""
    try:
        return _scan_pinned_tree(source_fd, toolchain=toolchain)
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


def _scan_pinned_tree(source_fd: int, *, toolchain: bool = False) -> set[tuple[int, int]]:
    """Own only the current ancestor chain while examining each pinned entry."""
    count = 0
    expected_mount = mount_id(source_fd)
    root_info = os.fstat(source_fd)

    def check_owner(info: os.stat_result) -> None:
        if toolchain and (info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
            raise NamespaceError(
                "toolchain must be owned by root/worker and not writable by others"
            )

    check_owner(root_info)
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
                raise TrustScanLimitError("writable tree exceeds trust scan limit")
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
                check_owner(info)
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
        table = read_mount_table()
    except PrivateMountError as exc:
        raise NamespaceError("cannot inspect host mount table") from exc
    filesystems = [(mount.identity, mount.point, mount.filesystem) for mount in table.values()]
    for _, mountpoint, _ in filesystems:
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


def _validate_visible_root(path: Path) -> Path:
    root = _absolute_path(path, "visible root")
    if not root.is_dir():
        raise NamespaceError("visible root must be a directory")
    if any(_within(root, hidden) for hidden in _NEVER_VISIBLE):
        raise NamespaceError("visible root is a host state or device tree")
    if root not in _SYSTEM_VISIBLE_ROOTS:
        _not_broad(root, "visible root")
    return root


def _masked_entries(base: Path, visible: tuple[Path, ...]) -> list[tuple[Path, bool]]:
    """Return operational and environment-file paths to hide, with directory flags.

    Covers a visible tree's top level and its immediate children, where project
    checkouts live. A symlinked entry masks its target when that is visible.
    """
    candidates = [base]
    try:
        children = list(base.iterdir())
    except OSError as exc:
        raise NamespaceError("cannot inspect visible tree") from exc
    if len(children) > _MAX_VISIBLE_CHILDREN:
        raise NamespaceError("visible tree has too many top-level entries to mask")
    candidates.extend(child for child in children if child.is_dir() and not child.is_symlink())
    masks: list[tuple[Path, bool]] = []
    for directory in candidates:
        try:
            names = [entry.name for entry in os.scandir(directory)]
        except OSError as exc:
            raise NamespaceError("cannot inspect visible tree") from exc
        selected = [directory / name for name in names if name.startswith(".env")]
        work = directory / ".hub" / "work"
        if work.exists() or work.is_symlink():
            selected.append(work)
        for entry in selected:
            target = entry
            if entry.is_symlink():
                try:
                    target = entry.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue  # A dangling link exposes no content.
                if not any(_within(target, root) for root in visible):
                    continue  # Outside targets stay absent in the new namespace.
            if target.is_dir():
                masks.append((target, True))
            elif target.is_file():
                masks.append((target, False))
            else:
                raise NamespaceError("visible tree contains an unsupported masked entry")
    return masks


def _validate_destinations(project: Path, roots: tuple[Path, ...], code_root: Path | None) -> None:
    """Reject ordinary mounts shadowing namespace-owned destinations.

    HOME is remapped, and the optional socket has one fixed /run destination.
    Deliberate readonly runtime nesting and the .git overlay remain valid.
    """
    destinations = (project, *roots, *((code_root,) if code_root is not None else ()))
    for destination in destinations:
        if destination == Path("/tmp") or any(
            _within(destination, reserved) or _within(reserved, destination)
            for reserved in map(Path, (SANDBOX_HOME, "/proc", "/dev", "/run"))
        ):
            raise NamespaceError("mount destination overlaps a namespace-owned path")
    for alias in (*map(Path, _SYSTEM_ALIASES), _SHELL_ALIAS):
        if _within(project, alias) or _within(alias, project):
            raise NamespaceError("project destination overlaps a system alias")


def _system_shell_alias_args(
    roots: tuple[Path, ...], code: Path | None, visible: tuple[Path, ...] = ()
) -> list[str]:
    """Recreate one host alias to an exact already-pinned readonly shell file."""
    sources = (*roots, *visible, *((code,) if code is not None else ()))
    if _SHELL_TARGET not in roots or any(_within(_SHELL_ALIAS, root) for root in sources):
        return []  # An immutable ancestor mount already supplies the alias.
    try:
        recognized = (
            _SHELL_ALIAS.is_symlink() and _SHELL_ALIAS.resolve(strict=True) == _SHELL_TARGET
        )
    except (OSError, RuntimeError):
        return []
    return ["--symlink", _SHELL_TARGET.name, str(_SHELL_ALIAS)] if recognized else []


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
    git_access: str = "read-only"
    toolchain: NamespaceToolchain | None = None
    visible_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.project_access not in ("read-only", "read-write"):
            raise NamespaceError("unsupported project access mode")
        if self.git_access not in ("read-only", "read-write"):
            raise NamespaceError("unsupported Git access mode")
        if self.git_access == "read-write" and self.project_access != "read-write":
            raise NamespaceError("writable Git requires a writable project")
        if self.network not in ("private", "shared"):
            raise NamespaceError("unsupported network mode")
        if self.network == "private" and self.permission_socket is not None:
            raise NamespaceError("private network profile cannot expose a permission socket")
        if len(set(self.visible_roots)) != len(self.visible_roots):
            raise NamespaceError("visible roots must be unique")

    def _validate(
        self,
        mount_ids: Mapping[Path, int],
        source_fds: Mapping[Path, int],
        private_guard: PrivateMountGuard | None = None,
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
        if not (
            stat.S_ISDIR(os.fstat(source_fds[git]).st_mode)
            or stat.S_ISREG(os.fstat(source_fds[git]).st_mode)
        ):
            raise NamespaceError("project must have supported Git metadata")
        home_stat = os.fstat(source_fds[home])
        home_identity = (home_stat.st_dev, home_stat.st_ino)
        for protected in (project, git):
            protected_stat = os.fstat(source_fds[protected])
            if home_identity == (protected_stat.st_dev, protected_stat.st_ino):
                raise NamespaceError("protected project and session-home mount roles overlap")
        if home_stat.st_uid != os.geteuid() or stat.S_IMODE(home_stat.st_mode) != 0o700:
            raise NamespaceError("session home must be owned by worker UID and mode 0700")
        visible = tuple(_validate_visible_root(root) for root in self.visible_roots)
        # Visible trees are recursive readonly views, not trust-scanned sources.
        _reject_nested_mounts(
            *(path for path in source_fds if path not in visible), mount_ids=mount_ids
        )
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
            if socket_stat.st_nlink != 1:
                raise NamespaceError("permission socket has an unsupported hardlink")
            parent_stat = sock.parent.stat()
            if (
                socket_stat.st_uid != os.geteuid()
                or stat.S_IMODE(socket_stat.st_mode) != 0o600
                or parent_stat.st_uid != os.geteuid()
                or stat.S_IMODE(parent_stat.st_mode) != 0o700
            ):
                raise NamespaceError("permission socket and parent need private ownership and mode")
        bwrap, roots = self.runtime.validate()
        tools = () if self.toolchain is None else self.toolchain.roots
        for index, path in enumerate(tools):
            _absolute_path(path, "toolchain root")
            _not_broad(path, "toolchain root")
            if any(
                _within(path, root) or _within(root, path)
                for root in (
                    *roots,
                    *tools[:index],
                    *((self.runtime.code_root,) if self.runtime.code_root else ()),
                )
            ):
                raise NamespaceError("toolchain and trusted runtime mounts overlap")
            _scan_writable_tree(path, source_fds[path], toolchain=True)
        code = self.runtime.code_root
        sources = (project, home, *roots, *tools, *((code,) if code is not None else ()))
        for writable in (project, home):
            for other in sources:
                if writable != other and (_within(writable, other) or _within(other, writable)):
                    raise NamespaceError("writable and other mounts overlap")
        for root in visible:
            # The writable project may sit inside a visible tree: it is bound later.
            if _within(root, project) or _within(home, root) or _within(root, home):
                raise NamespaceError("visible and writable mounts overlap")
            if sock is not None and (_within(sock, root) or _within(root, sock)):
                raise NamespaceError("permission socket overlaps a visible tree")
        if not self.private_paths:
            raise NamespaceError("private authority paths must be explicit")
        for raw in self.private_paths:
            private = _absolute_path(raw, "private authority path", exists=False)
            _not_broad(private, "private authority path")
            exposed = tuple(source_fds)
            if any(_within(source, private) or _within(private, source) for source in exposed):
                raise NamespaceError("mount overlaps private authority")
        if sock is not None and any(
            _within(source, sock) or _within(sock, source) for source in sources
        ):
            raise NamespaceError("permission socket overlaps a broad mount")
        _validate_destinations(project, (*roots, *tools, *visible), code)
        if self.toolchain is not None:
            try:
                validate_control_interpreters(
                    (self.runtime.executable, *self.runtime.auxiliary_executables), roots
                )
            except (OSError, ValueError) as exc:
                raise NamespaceError("toolchain cannot select a control interpreter") from exc
            for path, project_hook in (
                *((path, False) for path in self.toolchain.path),
                (self.toolchain.git_hooks_path, True),
                (self.toolchain.git_python, False),
                (self.toolchain.author_policy, False),
            ):
                if path is None:
                    continue
                visible = (*roots, *tools)
                if project_hook:
                    visible = (project, *visible)
                if not any(_within(path, root) for root in visible):
                    raise NamespaceError("toolchain setting is outside visible mounts")
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise NamespaceError("toolchain setting is unavailable") from None
                if not any(_within(target, root) for root in visible) or _within(
                    target, project / ".hub/work"
                ):
                    raise NamespaceError("toolchain setting resolves outside visible mounts")
                if path in self.toolchain.path and not path.is_dir():
                    raise NamespaceError("toolchain PATH entry must be a directory")
                if path in self.toolchain.path:
                    for entry in path.iterdir():
                        if entry.is_symlink():
                            try:
                                target = entry.resolve(strict=False)
                            except (OSError, RuntimeError) as exc:
                                raise NamespaceError("unsafe toolchain PATH link") from exc
                            if not any(_within(target, root) for root in (*roots, *tools)):
                                raise NamespaceError("toolchain PATH link leaves readonly mounts")
                if path == self.toolchain.git_hooks_path and not path.is_dir():
                    raise NamespaceError("Git hooks path must be a directory")
                if path == self.toolchain.git_python and (
                    not path.is_file() or not os.access(path, os.X_OK)
                ):
                    raise NamespaceError("Git Python must be executable")
        try:
            if private_guard is None:
                with PrivateMountGuard() as guard:
                    guard.check(source_fds, mount_ids, self.private_paths)
                    guard.recheck()
            else:
                private_guard.check(source_fds, mount_ids, self.private_paths)
        except (PrivateMountError, MountPinError) as exc:
            raise NamespaceError(str(exc)) from exc
        return bwrap, project, home, roots, sock

    def wrap(self, argv: Sequence[str], env: Mapping[str, str], cwd: Path | str) -> SandboxLaunch:
        """Pin, validate and transfer descriptor ownership to the existing caller."""
        pins = MountPins()
        try:
            with PrivateMountGuard() as private_guard:
                command, child = self._build(argv, env, cwd, pins, private_guard)
                pins.recheck()
                private_guard.recheck()
            return SandboxLaunch(tuple(command), child, pins)
        except BaseException as exc:
            pins.close()
            if isinstance(exc, (MountPinError, OSError)):
                raise NamespaceError("mount sources could not be pinned") from exc
            if isinstance(exc, PrivateMountError):
                raise NamespaceError(str(exc)) from exc
            raise

    def _build(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: Path | str,
        pins: MountPins,
        private_guard: PrivateMountGuard,
    ) -> tuple[list[str], dict[str, str]]:
        project_fd = pins.open(Path(self.project_root), directory=True)
        git_sources = pin_git_sources(pins, project_fd, Path(self.project_root))
        home_fd = pins.open(Path(self.session_home), directory=True)
        sock_fd = (
            pins.open(Path(self.permission_socket)) if self.permission_socket is not None else None
        )
        runtime_fds = {Path(root): pins.open(Path(root)) for root in self.runtime.runtime_roots}
        tools = () if self.toolchain is None else self.toolchain.roots
        tool_fds = {root: pins.open(root, directory=True) for root in tools}
        visible_fds = {
            Path(root): pins.open(Path(root), directory=True) for root in self.visible_roots
        }
        code = self.runtime.code_root
        code_fd = None
        if code is not None and not any(_within(code, root) for root in runtime_fds):
            code_fd = pins.open(code, directory=True)
        source_fds = {
            Path(self.project_root): project_fd,
            **git_sources.descriptors,
            Path(self.session_home): home_fd,
            **runtime_fds,
            **tool_fds,
            **visible_fds,
        }
        if self.permission_socket is not None and sock_fd is not None:
            source_fds[Path(self.permission_socket)] = sock_fd
        if code is not None and code_fd is not None:
            source_fds[code] = code_fd
        external = tuple(
            path for path in git_sources.descriptors if path != Path(self.project_root) / ".git"
        )
        if external:
            _validate_destinations(Path(self.project_root), external, code)
            for path in external:
                _not_broad(path, "Git metadata")
                for other in (
                    Path(self.project_root),
                    Path(self.session_home),
                    *runtime_fds,
                    *tool_fds,
                    *((code,) if code is not None else ()),
                ):
                    if _within(path, other) or _within(other, path):
                        raise NamespaceError("external Git and other mounts overlap")
        bwrap, project, home, roots, sock = self._validate(
            {path: pins.mount_id(fd) for path, fd in source_fds.items()}, source_fds, private_guard
        )
        for directory in git_sources.external_directories:
            identities = _scan_writable_tree(directory, source_fds[directory])
            home_info = os.fstat(home_fd)
            if (home_info.st_dev, home_info.st_ino) in identities:
                raise NamespaceError("Git and session-home mounts overlap")
        git_sources.recheck()
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
            HOME=SANDBOX_HOME,
            XDG_CONFIG_HOME=f"{SANDBOX_HOME}/.config",
            XDG_CACHE_HOME=f"{SANDBOX_HOME}/.cache",
            XDG_STATE_HOME=f"{SANDBOX_HOME}/.local/state",
            XDG_DATA_HOME=f"{SANDBOX_HOME}/.local/share",
            TMPDIR="/tmp",
            PATH="/usr/bin:/bin",
        )
        if git_sources.controls:
            child_env.update(
                git_environment(
                    {"gc.auto": "0", "maintenance.auto": "false", "branch.autoSetupMerge": "false"}
                )
            )
        if self.toolchain is not None:
            child_env["PATH"] = ":".join((*map(str, self.toolchain.path), "/usr/bin", "/bin"))
            child_env.update(self.toolchain.environment())
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
        # Wide views first: pinned runtime/tool mounts then shadow their subtrees.
        for root, fd in visible_fds.items():
            command.extend(("--ro-bind-fd", str(fd), str(root)))
        for root in roots:
            command.extend(("--ro-bind-fd", str(runtime_fds[root]), str(root)))
        for root, fd in tool_fds.items():
            command.extend(("--ro-bind-fd", str(fd), str(root)))
        for alias, target in _SYSTEM_ALIASES.items():
            alias_path = Path(alias)
            if (
                alias_path.is_symlink()
                and alias_path.resolve() == Path(target)
                and (
                    any(_within(root, Path(target)) for root in roots)
                    or any(_within(Path(target), root) for root in visible_fds)
                )
            ):
                command.extend(("--symlink", target.lstrip("/"), alias))
        command.extend(_system_shell_alias_args(roots, code, tuple(visible_fds)))
        if code is not None and code_fd is not None:
            command.extend(("--ro-bind-fd", str(code_fd), str(code)))
        project_flag = "--ro-bind-fd" if self.project_access == "read-only" else "--bind-fd"
        command.extend(
            (
                project_flag,
                str(project_fd),
                str(project),
            )
        )
        command.extend(git_sources.arguments(self.git_access))
        command.extend(("--bind-fd", str(home_fd), SANDBOX_HOME))
        # Operational work products may contain private backups/review receipts.
        # Keep materials and deliverable staging usable, but hide this subtree.
        operational = project / ".hub" / "work"
        if operational.exists() or operational.is_symlink():
            _absolute_path(operational, "operational workspace")
            if not operational.is_dir():
                raise NamespaceError("operational workspace must be a directory")
            command.extend(("--tmpfs", str(operational), "--remount-ro", str(operational)))
        if visible_fds:
            visible = (*visible_fds, project)
            hidden = {operational}
            for base in visible:
                for path, directory in _masked_entries(base, visible):
                    if path in hidden:
                        continue
                    hidden.add(path)
                    if directory:
                        command.extend(("--tmpfs", str(path), "--remount-ro", str(path)))
                    else:
                        command.extend(("--ro-bind", "/dev/null", str(path)))
        if sock is not None and sock_fd is not None:
            command.extend(("--ro-bind-fd", str(sock_fd), _SANDBOX_SOCKET))
        # Freeze synthetic ancestor directories and aliases as well as runtime
        # bytes. This is nonrecursive: explicit project/HOME/tmp mounts retain
        # their own access modes. A failed remount must fail the owned launch.
        command.extend(("--remount-ro", "/", "--chdir", str(workdir), "--", *argv))
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
