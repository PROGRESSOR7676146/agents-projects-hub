"""Explicit digest pins for sealed user-owned runtime versions (trusted host).

This protects against stale or accidentally changed releases, not a hostile
process sharing the owner's UID. Namespace mounts remain read-only separately.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_SHA256 = re.compile(r"[a-f0-9]{64}")
_MAX_ENTRIES = 100_000


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _canonical(path: Path) -> None:
    _require(
        path.is_absolute() and ".." not in path.parts and path.resolve() == path,
        "user release path must be absolute and canonical",
    )


def _no_acl(path: Path) -> None:
    try:
        _require(
            not {"system.posix_acl_access", "system.posix_acl_default"}.intersection(
                os.listxattr(path, follow_symlinks=False)
            ),
            "user release has unsupported ACLs",
        )
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise ValueError("user release ACLs cannot be inspected") from error


def _ancestors(root: Path) -> None:
    _canonical(root)
    _require(
        root.is_dir() and root.parent.name == "releases",
        "user release must be a version directory under releases",
    )
    _require(
        os.geteuid() != 0 and root.stat().st_uid == os.geteuid(),
        "user release must belong to the unprivileged worker user",
    )
    for path in root.parents:
        info = path.stat()
        sticky_system_directory = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        _require(
            info.st_uid in {0, os.geteuid()}
            and (not info.st_mode & 0o022 or sticky_system_directory),
            "user release ancestor has unsafe ownership or permissions",
        )
        _no_acl(path)


def _entries(root: Path) -> list[Path]:
    paths = [root]
    pending = [root]
    while pending:
        directory = pending.pop()
        _require(len(directory.relative_to(root).parts) < 128, "user release exceeds depth limit")
        for path in directory.iterdir():
            _require(len(paths) < _MAX_ENTRIES, "user release exceeds entry limit")
            paths.append(path)
            if stat.S_ISDIR(path.lstat().st_mode):
                pending.append(path)
    return [root, *sorted(paths[1:])]


def _symlink(path: Path, root: Path) -> str:
    literal = os.readlink(path)
    raw = Path(literal)
    target = path.resolve(strict=True)
    lexical = Path(os.path.normpath(path.parent / raw))
    internal = (lexical == root or root in lexical.parents) and (
        target == root or root in target.parents
    )
    if internal:
        # Check each hop: an OS directory link plus .. can otherwise leave the
        # release and return through a mutable current link with both endpoints inside.
        _require(
            not raw.is_absolute() or raw.is_relative_to(root),
            "user release symlink escapes runtime",
        )
        cursor = root if raw.is_absolute() else path.parent
        parts = raw.relative_to(root).parts if raw.is_absolute() else raw.parts
        for part in parts:
            cursor = (cursor / part).resolve(strict=True)
            _require(
                cursor == root or root in cursor.parents, "user release symlink escapes runtime"
            )
    else:
        _require(
            raw.is_absolute() and Path("/usr") in raw.parents and Path("/usr") in target.parents,
            "user release symlink escapes runtime",
        )
        for parent in (target, *target.parents):
            info = parent.stat()
            _require(
                info.st_uid == 0
                and not info.st_mode & 0o022
                and not os.access(parent, os.W_OK, effective_ids=True),
                "user release symlink requires immutable OS code",
            )
            _no_acl(parent)
    return literal


def release_digest(root: Path) -> str:
    """Hash newline-delimited JSON [path, mode, type, payload] records.

    File payloads are SHA-256 digests; link payloads are literal link targets.
    The standalone updater implements this format without importing Hub.
    """
    _ancestors(root)
    return _tree_digest(root)


def installed_runtime_digest(root: Path) -> str:
    """Inspect an already installed sealed user/root-owned runtime, without writes.

    This does not relax UserRuntimeRelease's user-owned direct-child contract.
    The installed release procedure separately binds the digest to provenance.
    """
    _canonical(root)
    _require(root.is_dir(), "installed runtime must be a directory")
    for path in (root, *root.parents):
        info = path.stat()
        sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        _require(
            info.st_uid in {0, os.geteuid()} and (not info.st_mode & 0o022 or sticky),
            "installed runtime has unsafe ancestors",
        )
        _no_acl(path)
    return _tree_digest(root)


def _tree_digest(root: Path) -> str:
    result = hashlib.sha256()
    for path in _entries(root):
        info = path.lstat()
        _require(info.st_uid in {0, os.geteuid()}, "user release entry has an untrusted owner")
        _no_acl(path)
        record: list[Any] = [str(path.relative_to(root)), stat.S_IMODE(info.st_mode)]
        if stat.S_ISLNK(info.st_mode):
            record.extend(("link", _symlink(path, root)))
        else:
            _require(
                not info.st_mode & 0o022 and not os.access(path, os.W_OK, effective_ids=True),
                "user release must be sealed read-only",
            )
            if stat.S_ISREG(info.st_mode):
                _require(info.st_nlink == 1, "user release contains a hardlink")
                content = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        content.update(chunk)
                record.extend(("file", content.hexdigest()))
            else:
                _require(stat.S_ISDIR(info.st_mode), "user release contains a special file")
                record.append("directory")
        result.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    return result.hexdigest()


@dataclass(frozen=True, slots=True)
class UserRuntimeRelease:
    root: Path
    sha256: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.root, Path)
            and self.root.is_absolute()
            and ".." not in self.root.parts
            and "\x00" not in str(self.root),
            "user release root must be an absolute version path",
        )
        _require(
            isinstance(self.sha256, str) and _SHA256.fullmatch(self.sha256),
            "user release SHA-256 is invalid",
        )

    def verify(self) -> None:
        _require(release_digest(self.root) == self.sha256, "user release digest changed")


def parse_user_releases(raw: object) -> tuple[UserRuntimeRelease, ...]:
    _require(
        isinstance(raw, list) and 1 <= len(raw) <= 8,
        "user_releases requires 1..8 explicit release pins",
    )
    pins = []
    assert isinstance(raw, list)
    for item in raw:
        _require(
            isinstance(item, dict)
            and set(item) == {"root", "sha256"}
            and isinstance(item["root"], str),
            "user release pin requires root and sha256",
        )
        pin = UserRuntimeRelease(Path(item["root"]), item["sha256"])
        _require(
            not any(
                pin.root == old.root or pin.root in old.root.parents or old.root in pin.root.parents
                for old in pins
            ),
            "user release roots must be unique and disjoint",
        )
        pins.append(pin)
    return tuple(pins)


def seal_release(root: Path) -> UserRuntimeRelease:
    """Seal a prepared owned version; never copy, install or follow links."""
    _ancestors(root)
    paths = _entries(root)
    for path in paths:
        info = path.lstat()
        _require(info.st_uid == os.geteuid(), "sealing requires exclusively user-owned entries")
        _no_acl(path)
        if stat.S_ISLNK(info.st_mode):
            _symlink(path, root)
        else:
            _require(
                stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
                "sealing refuses hardlinks and special files",
            )
    for path in reversed(paths):
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    return UserRuntimeRelease(root, release_digest(root))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pin", "seal", "verify"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args(argv)
    if args.action == "verify" and args.sha256 is None:
        parser.error("verify requires --sha256")
    try:
        pin = (
            seal_release(args.root)
            if args.action == "seal"
            else UserRuntimeRelease(
                args.root, args.sha256 if args.action == "verify" else release_digest(args.root)
            )
        )
        pin.verify()
    except (ValueError, OSError, RuntimeError):
        print(
            json.dumps({"ok": False, "reason": "release ownership, sealing or digest check failed"})
        )
        return 1
    print(json.dumps({"ok": True, "root": str(pin.root), "sha256": pin.sha256}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
