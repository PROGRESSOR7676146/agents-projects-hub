"""Casefold refusal uses explicit filesystem evidence, never dentry spelling."""

from __future__ import annotations

import errno
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hermes_codex_router.mount_lookup as lookup
from hermes_codex_router.claude_mount_pins import MountPinError, MountPins
from hermes_codex_router.review_materials import (
    MaterialSelection,
    ReviewMaterialError,
    build_review_capsule,
)
from tests.fd_fixture import assert_descriptor_cleanup


class MountLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-lookup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "пример"
        self.root.mkdir()
        self.fd = os.open(self.root, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        self.addCleanup(os.close, self.fd)

    def test_unsupported_abi_refuses_before_native_inspection(self) -> None:
        with (
            patch.object(lookup.os, "uname", return_value=SimpleNamespace(machine="i686")),
            patch.object(lookup.ctypes, "CDLL") as libc,
            self.assertRaisesRegex(lookup.LookupEvidenceError, "ABI.*unsupported"),
        ):
            lookup.require_case_sensitive_directory(self.fd)
        libc.assert_not_called()

    def test_missing_or_failed_native_filesystem_evidence_refuses_and_closes_fd(self) -> None:
        with assert_descriptor_cleanup(self):
            with (
                patch.object(lookup.ctypes, "CDLL", return_value=object()),
                self.assertRaisesRegex(lookup.LookupEvidenceError, "evidence.*unavailable"),
            ):
                lookup.require_case_sensitive_directory(self.fd)
        with assert_descriptor_cleanup(self):
            with (
                patch.object(lookup.ctypes, "CDLL") as libc,
                patch.object(lookup.ctypes, "get_errno", return_value=errno.EIO),
            ):
                libc.return_value.fstatfs.return_value = -1
                with self.assertRaises(lookup.LookupEvidenceError) as error:
                    lookup.require_case_sensitive_directory(self.fd)
                self.assertIsInstance(error.exception.__cause__, OSError)
                self.assertEqual(error.exception.__cause__.errno, errno.EIO)

    def test_case_sensitive_supported_filesystems_accept_and_close_inspection_fd(self) -> None:
        for kind in (lookup._EXT, lookup._TMPFS, lookup._BTRFS):
            with self.subTest(kind=kind), assert_descriptor_cleanup(self):
                with (
                    patch.object(lookup, "_filesystem_type", return_value=kind),
                    patch.object(lookup.fcntl, "ioctl", return_value=struct.pack("=I", 0)),
                ):
                    lookup.require_case_sensitive_directory(self.fd)
        os.fstat(self.fd)

    def test_casefold_errors_unknown_filesystems_and_malformed_flags_refuse(self) -> None:
        for value in (struct.pack("=I", lookup._CASEFOLD), b"", bytes(8), 0):
            with self.subTest(value=value), assert_descriptor_cleanup(self):
                with (
                    patch.object(lookup, "_filesystem_type", return_value=lookup._EXT),
                    patch.object(lookup.fcntl, "ioctl", return_value=value),
                    self.assertRaises(lookup.LookupEvidenceError),
                ):
                    lookup.require_case_sensitive_directory(self.fd)
        for number in (errno.ENOTTY, errno.EOPNOTSUPP, errno.ENOENT):
            with self.subTest(number=number), assert_descriptor_cleanup(self):
                with (
                    patch.object(lookup, "_filesystem_type", return_value=lookup._TMPFS),
                    patch.object(lookup.fcntl, "ioctl", side_effect=OSError(number, "fictional")),
                    self.assertRaises(lookup.LookupEvidenceError) as raised,
                ):
                    lookup.require_case_sensitive_directory(self.fd)
                self.assertIsInstance(raised.exception.__cause__, OSError)
        with patch.object(lookup, "_filesystem_type", return_value=0x794C7630):
            with self.assertRaisesRegex(lookup.LookupEvidenceError, "unsupported"):
                lookup.require_case_sensitive_directory(self.fd)

    def test_xfs_geometry_ascii_ci_and_version_bounds_use_their_own_ioctl(self) -> None:
        for version, flags, accepted in (
            (0, 0, True),
            (0, 1 << 12, False),
            (4, 0, False),
            (6, 0, False),
        ):
            data = bytearray(112)
            struct.pack_into("=iI", data, 88, version, flags)
            with self.subTest(version=version, flags=flags), assert_descriptor_cleanup(self):
                with (
                    patch.object(lookup, "_filesystem_type", return_value=lookup._XFS),
                    patch.object(lookup.fcntl, "ioctl", return_value=bytes(data)) as ioctl,
                ):
                    if accepted:
                        lookup.require_case_sensitive_directory(self.fd)
                    else:
                        with self.assertRaises(lookup.LookupEvidenceError):
                            lookup.require_case_sensitive_directory(self.fd)
                    self.assertEqual(ioctl.call_args.args[1], 0x80705864)
                    self.assertEqual(len(ioctl.call_args.args[2]), 112)

    def test_real_case_sensitive_unicode_paths_remain_supported(self) -> None:
        (self.root / "обзор.txt").write_text("visible text", encoding="utf-8")
        with MountPins() as pins:
            parent = pins.open(self.root, directory=True)
            child = pins.open_relative(parent, "обзор.txt", directory=False)
            self.assertEqual(os.fstat(child).st_ino, (self.root / "обзор.txt").stat().st_ino)
            pins.recheck()

    def test_inspection_reopens_pinned_dot_readonly_and_unreadable_refuses(self) -> None:
        real_open = os.open
        with patch.object(lookup.os, "open", wraps=real_open) as opened:
            lookup.require_case_sensitive_directory(self.fd)
        self.assertEqual(opened.call_args.args, (".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        self.assertEqual(opened.call_args.kwargs, {"dir_fd": self.fd})
        with patch.object(lookup.os, "open", side_effect=PermissionError("fictional")):
            with self.assertRaisesRegex(lookup.LookupEvidenceError, "unavailable"):
                lookup.require_case_sensitive_directory(self.fd)

    def test_casefold_root_blocks_ignorable_name_before_selected_open_or_read(self) -> None:
        # Matching dentry spelling does not bypass flagged-parent refusal.
        for name in ("visible.txt", ".g\u200cit/config"):
            with self.subTest(name=name), assert_descriptor_cleanup(self):
                with (
                    patch.object(lookup, "_filesystem_type", return_value=lookup._EXT),
                    patch.object(
                        lookup.fcntl, "ioctl", return_value=struct.pack("=I", lookup._CASEFOLD)
                    ),
                    patch("hermes_codex_router.review_materials._read_selected") as read,
                ):
                    with self.assertRaises(ReviewMaterialError) as raised:
                        build_review_capsule(
                            self.root,
                            (MaterialSelection(name, 0, "0" * 64),),
                            binding="example-result",
                        )
                    self.assertIsInstance(raised.exception.__cause__, MountPinError)
                    self.assertIsInstance(
                        raised.exception.__cause__.__cause__, lookup.LookupEvidenceError
                    )
                    read.assert_not_called()
