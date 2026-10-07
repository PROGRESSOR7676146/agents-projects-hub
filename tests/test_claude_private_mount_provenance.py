"""Fail-closed provenance, missing-path errors, and validation fd ownership."""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.claude_private_mounts as private_mounts
from hermes_codex_router.claude_mount_pins import MountPins
from hermes_codex_router.claude_private_mounts import PrivateMountError, PrivateMountGuard


class PrivateMountProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="example-provenance-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "project"
        self.source.mkdir()
        self.private = self.base / "authority"
        self.private.mkdir()

    def check(self, private: Path | None = None) -> None:
        with MountPins() as pins, PrivateMountGuard() as guard:
            fd = pins.open(self.source)
            guard.check(
                {self.source: fd}, {self.source: pins.mount_id(fd)}, (private or self.private,)
            )
            guard.recheck()

    def test_malformed_ambiguous_or_unavailable_table_refuses_and_closes_fds(self) -> None:
        device = self.source.stat().st_dev
        row = f"1 0 {os.major(device)}:{os.minor(device)} / / rw - ext4 example rw\n"
        for table in (
            "",
            row + row,
            row.replace("1 0", "0 0"),
            row.replace(" / / ", " relative / "),
            row.replace(" / / ", " /x/../secret / "),
            row.replace(" / / ", " /x\\999 / "),
            row.replace("ext4 example rw", "ext4"),
        ):
            with self.subTest(table=table):
                before = len(os.listdir("/proc/self/fd"))
                original = Path.read_text

                def read(path: Path, *args: object, **kwargs: object) -> str:
                    if path == Path("/proc/self/mountinfo"):
                        return table
                    return original(path, *args, **kwargs)  # type: ignore[arg-type]

                with (
                    patch.object(Path, "read_text", read),
                    patch.object(private_mounts, "_read_mountinfo", return_value=table),
                ):
                    with self.assertRaises(PrivateMountError):
                        self.check()
                self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_selected_mount_device_must_match_actual_descriptor(self) -> None:
        original = Path.read_text
        table = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        with MountPins() as pins:
            fd = pins.open(self.source)
            identity = pins.mount_id(fd)
        rows = []
        for line in table.splitlines():
            fields = line.split()
            if fields[0] == str(identity):
                fields[2] = "0:999"
            rows.append(" ".join(fields))

        def read(path: Path, *args: object, **kwargs: object) -> str:
            if path == Path("/proc/self/mountinfo"):
                return "\n".join(rows)
            return original(path, *args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(Path, "read_text", read),
            patch.object(private_mounts, "_read_mountinfo", return_value="\n".join(rows)),
        ):
            with self.assertRaisesRegex(PrivateMountError, "does not match its pin"):
                self.check()

    def test_equal_private_file_inode_is_refused_even_with_different_names(self) -> None:
        private_file = self.private / "key"
        private_file.write_text("fictional key", encoding="utf-8")
        alias = self.base / "readonly-runtime"
        os.link(private_file, alias)
        before = len(os.listdir("/proc/self/fd"))
        with MountPins() as pins, PrivateMountGuard() as guard:
            fd = pins.open(alias)
            with self.assertRaisesRegex(PrivateMountError, "mount overlaps private authority"):
                guard.check({alias: fd}, {alias: pins.mount_id(fd)}, (private_file,))
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_escaped_filesystem_coordinates_and_unrelated_nsfs_are_supported(self) -> None:
        table = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        identity = max(int(line.split()[0]) for line in table.splitlines()) + 1
        device = self.source.stat().st_dev
        table += (
            f"{identity} 1 {os.major(device)}:{os.minor(device)} "
            f"/independent\\040materials\\134copy {self.source} rw - ext4 example rw\n"
            f"{identity + 1} 1 0:999 net:[123] /run/example-namespace rw - nsfs nsfs rw\n"
        )
        with (
            MountPins() as pins,
            PrivateMountGuard() as guard,
            patch.object(private_mounts, "_read_mountinfo", return_value=table),
        ):
            fd = pins.open(self.source)
            guard.check({self.source: fd}, {self.source: identity}, (self.private,))
            guard.recheck()

    def test_mount_table_read_errors_and_size_bound_refuse(self) -> None:
        for error in (PermissionError("fixture"), UnicodeError("fixture")):
            with patch.object(private_mounts, "_read_mountinfo", side_effect=error):
                with self.assertRaisesRegex(PrivateMountError, "unavailable"):
                    self.check()
        with patch.object(private_mounts, "_MAX_TABLE_BYTES", 1):
            with self.assertRaisesRegex(PrivateMountError, "exceeds its bound"):
                self.check()

    def test_partial_private_pin_failure_closes_prior_validation_handles(self) -> None:
        (self.private / "file").write_text("fictional material", encoding="utf-8")
        before = len(os.listdir("/proc/self/fd"))
        with MountPins() as pins, PrivateMountGuard() as guard:
            fd = pins.open(self.source)
            with self.assertRaisesRegex(PrivateMountError, "cannot be pinned"):
                guard.check(
                    {self.source: fd},
                    {self.source: pins.mount_id(fd)},
                    (self.private, self.private / "file" / "impossible"),
                )
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_permission_failure_is_not_a_missing_private_path(self) -> None:
        actual = MountPins.open

        def denied(pins: MountPins, path: Path, **kwargs: object) -> int:
            if path == self.private:
                from hermes_codex_router.claude_mount_pins import MountPinError

                raise MountPinError("fixture") from PermissionError(errno.EACCES, "fixture")
            return actual(pins, path, **kwargs)  # type: ignore[arg-type]

        before = len(os.listdir("/proc/self/fd"))
        with patch.object(MountPins, "open", denied):
            with self.assertRaisesRegex(PrivateMountError, "cannot be pinned"):
                self.check()
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
