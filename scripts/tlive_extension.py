#!/usr/bin/env python3
"""Apply and verify the pinned tlive 5.3.1 protected-permission source patch.

Only a caller-selected unpacked source tree is touched. No install, network,
service, credential, or inference operation is performed.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "integrations" / "tlive"
MANIFEST = PACKAGE / "source-manifest.json"
PATCH = PACKAGE / "tlive-5.3.1-protected-permissions.patch"
RENAME_EXCHANGE = 2
AT_FDCWD = -100


class SourceMismatch(ValueError):
    """Source bytes, tree shape, or patch hash differ from the pinned package."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["package"] != "tlive" or manifest["version"] != "5.3.1":
        raise SourceMismatch("unsupported manifest identity")
    if digest(PATCH.read_bytes()) != manifest["patch_sha256"]:
        raise SourceMismatch("patch bytes differ from manifest")
    return manifest


def source_path(argument: str) -> Path:
    path = Path(argument).absolute()
    if not path.is_dir() or path != path.resolve(strict=True):
        raise SourceMismatch("select a real unpacked tlive source directory without symlinks")
    return path


def checked_file(source: Path, relative: str) -> Path:
    path = source
    for part in Path(relative).parts:
        if part in ("", ".", ".."):
            raise SourceMismatch("unsafe manifest path")
        path = path / part
        if path.is_symlink():
            raise SourceMismatch(f"symlink in pinned source path: {relative}")
    return path


def verify_tree(source: Path, manifest: dict, *, patched: bool) -> None:
    expected = dict(manifest["base_files"])
    added = set(manifest["patched_files"]) - set(expected)
    if patched:
        expected.update(manifest["patched_files"])
    for relative, expected_hash in expected.items():
        path = checked_file(source, relative)
        if not path.is_file() or digest(path.read_bytes()) != expected_hash:
            raise SourceMismatch(f"pinned source mismatch: {relative}")
    if not patched:
        for relative in added:
            if checked_file(source, relative).exists():
                raise SourceMismatch(f"conflicting patch target: {relative}")
    # New source files change the reviewed program even if all known bytes match.
    expected_source = {name for name in expected if name.startswith("src/")}
    actual_source = {
        path.relative_to(source).as_posix()
        for path in (source / "src").rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_source != expected_source:
        raise SourceMismatch("source file set differs from pinned release")


def verify_archive(path: Path | None, manifest: dict) -> None:
    if path is not None and digest(path.read_bytes()) != manifest["archive_sha256"]:
        raise SourceMismatch("release archive SHA-256 mismatch")


def exchange_directories(left: Path, right: Path) -> None:
    """Linux atomic directory swap; fail closed if the filesystem lacks it."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError("atomic renameat2 exchange is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(AT_FDCWD, os.fsencode(left), AT_FDCWD, os.fsencode(right), RENAME_EXCHANGE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def apply(source: Path, manifest: dict) -> None:
    try:
        verify_tree(source, manifest, patched=True)
    except SourceMismatch:
        verify_tree(source, manifest, patched=False)
    else:
        return  # Already applied, exactly.

    # Build the complete replacement on the same filesystem before mutation.
    with tempfile.TemporaryDirectory(prefix=".tlive-stage-", dir=source.parent) as temp:
        staged = Path(temp) / "source"
        shutil.copytree(source, staged, symlinks=True)
        verify_tree(staged, manifest, patched=False)
        subprocess.run(
            ["git", "apply", "--check", str(PATCH)], cwd=staged, check=True, capture_output=True
        )
        subprocess.run(["git", "apply", str(PATCH)], cwd=staged, check=True, capture_output=True)
        verify_tree(staged, manifest, patched=True)
        exchange_directories(source, staged)
        try:
            verify_tree(source, manifest, patched=True)
        except BaseException:
            exchange_directories(source, staged)
            raise
        # staged now holds the original tree; TemporaryDirectory removes it.


def build(source: Path, manifest: dict) -> None:
    verify_tree(source, manifest, patched=True)
    subprocess.run(["node", "scripts/build.mjs"], cwd=source, check=True)
    verify_tree(source, manifest, patched=True)
    daemon = source / "dist" / "src" / "tlive-daemon.mjs"
    cli = source / "dist" / "src" / "tlive-cli.mjs"
    if not daemon.is_file() or not cli.is_file():
        raise SourceMismatch("tlive build did not produce daemon and CLI")
    if b"hub.permission.hello" not in daemon.read_bytes():
        raise SourceMismatch("daemon build lacks protected capability")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check-base", "apply", "verify", "build"))
    parser.add_argument("source", help="caller-selected unpacked tlive 5.3.1 package directory")
    parser.add_argument("--archive", type=Path, help="optional original tgz to SHA-256 verify")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest()
        verify_archive(args.archive, manifest)
        source = source_path(args.source)
        if args.action == "check-base":
            verify_tree(source, manifest, patched=False)
        elif args.action == "apply":
            apply(source, manifest)
        elif args.action == "verify":
            verify_tree(source, manifest, patched=True)
        else:
            build(source, manifest)
    except (SourceMismatch, OSError, subprocess.CalledProcessError, KeyError, ValueError) as error:
        print(f"tlive extension: {error}", file=sys.stderr)
        return 1
    print(f"tlive extension: {args.action} OK (tlive 5.3.1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
