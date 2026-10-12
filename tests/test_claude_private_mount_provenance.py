"""Fail-closed provenance, missing-path errors, and validation fd ownership."""

from __future__ import annotations

import errno
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.claude_private_mounts as private_mounts
from hermes_codex_router.claude_mount_pins import MountPinError, MountPins
from hermes_codex_router.claude_private_mounts import PrivateMountError, PrivateMountGuard
from hermes_codex_router.mount_lookup import LookupEvidenceError
from hermes_codex_router.process_namespace import NamespaceError as FileToolSandboxError
from hermes_codex_router.process_namespace import _reject_nested_mounts
from tests.fd_fixture import assert_descriptor_cleanup


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
                with assert_descriptor_cleanup(self):
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

    def test_selected_mount_device_must_match_actual_descriptor(self) -> None:
        original = Path.read_text
        table = Path("/proc/self/mountinfo").read_bytes().decode("utf-8")
        with MountPins() as pins:
            fd = pins.open(self.source)
            identity = pins.mount_id(fd)
        rows = []
        for line in table.removesuffix("\n").split("\n"):
            fields = line.split(" ")
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

    def test_kernel_descriptor_path_divergence_refuses_sources_and_private_anchors(self) -> None:
        (self.source / ".git").mkdir()
        for path in (self.source, self.source / ".git", self.private, self.base):
            for suffix in ("casefold", " (deleted)"):
                with self.subTest(path=path, suffix=suffix):
                    original = os.readlink
                    with assert_descriptor_cleanup(self):

                        def divergent(name: str, *args: object, **kwargs: object) -> str:
                            value = original(name, *args, **kwargs)  # type: ignore[arg-type]
                            if value != str(path):
                                return value
                            return (
                                str(path.with_name(path.name.upper()))
                                if suffix == "casefold"
                                else value + suffix
                            )

                        with (
                            MountPins() as pins,
                            PrivateMountGuard() as guard,
                            patch.object(os, "readlink", divergent),
                        ):
                            fds = {p: pins.open(p) for p in (self.source, self.source / ".git")}
                            authority = (
                                self.base / "missing" / "authority"
                                if path == self.base
                                else self.private
                            )
                            with self.assertRaisesRegex(
                                PrivateMountError, "does not match its pin"
                            ):
                                guard.check(
                                    fds,
                                    {p: pins.mount_id(fd) for p, fd in fds.items()},
                                    (authority,),
                                )

    def test_kernel_path_recheck_divergence_and_unavailability_refuse(self) -> None:
        (self.source / ".git").mkdir()
        for path in (self.source, self.source / ".git", self.private, self.base):
            for effect in ("casefold", "unavailable"):
                with self.subTest(path=path, effect=effect):
                    with assert_descriptor_cleanup(self):
                        with MountPins() as pins, PrivateMountGuard() as guard:
                            fds = {p: pins.open(p) for p in (self.source, self.source / ".git")}
                            authority = (
                                self.base / "missing" / "authority"
                                if path == self.base
                                else self.private
                            )
                            guard.check(
                                fds, {p: pins.mount_id(fd) for p, fd in fds.items()}, (authority,)
                            )
                            original = os.readlink

                            def changed(name: str, *args: object, **kwargs: object) -> str:
                                value = original(name, *args, **kwargs)  # type: ignore[arg-type]
                                if value != str(path):
                                    return value
                                if effect == "unavailable":
                                    raise PermissionError("fictional descriptor error")
                                return str(path.with_name(path.name.upper()))

                            with patch.object(os, "readlink", changed):
                                with self.assertRaises(PrivateMountError):
                                    guard.recheck()

    def test_equal_private_file_inode_is_refused_even_with_different_names(self) -> None:
        private_file = self.private / "key"
        private_file.write_text("fictional key", encoding="utf-8")
        alias = self.base / "readonly-runtime"
        os.link(private_file, alias)
        with assert_descriptor_cleanup(self):
            with MountPins() as pins, PrivateMountGuard() as guard:
                fd = pins.open(alias)
                with self.assertRaisesRegex(PrivateMountError, "mount overlaps private authority"):
                    guard.check({alias: fd}, {alias: pins.mount_id(fd)}, (private_file,))

    def test_escaped_filesystem_coordinates_and_unrelated_nsfs_are_supported(self) -> None:
        table = Path("/proc/self/mountinfo").read_bytes().decode("utf-8")
        identity = max(int(line.split(" ")[0]) for line in table.removesuffix("\n").split("\n")) + 1
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

    def test_unescaped_kernel_whitespace_remains_path_data(self) -> None:
        for character in ("\u00a0", "\u3000", "\x1f", "\r", "\u2028", "\v", "\f", "\x85"):
            with self.subTest(character=repr(character)):
                root = Path(f"/example/source{character}/subtree")
                point = Path(f"/example/view{character}/mount")
                row = f"1 0 0:1 {root} {point} rw shared:2 master:3 - tmpfs example rw\n"
                with patch.object(private_mounts, "_read_mountinfo", return_value=row):
                    mount = private_mounts.read_mount_table()[1]
                    self.assertEqual(mount.root, root)
                    self.assertEqual(mount.point, point)

    def test_unescaped_private_descendant_mount_is_not_lost_by_either_consumer(self) -> None:
        table = Path("/proc/self/mountinfo").read_bytes().decode("utf-8")
        identity = max(int(line.split(" ")[0]) for line in table.split("\n") if line) + 1
        for character in ("\u00a0", "\u3000", "\x1f", "\r", "\u2028", "\v", "\f", "\x85"):
            with self.subTest(character=repr(character)):
                private = self.base / f"auth{character}ority"
                private.mkdir()
                nested = private / "creds"
                nested.mkdir()
                rows = table + f"{identity} 1 0:999 / {nested} rw - tmpfs example rw\n"
                with assert_descriptor_cleanup(self):
                    original_read = Path.read_text

                    def read(path: Path, *args: object, **kwargs: object) -> str:
                        if path == Path("/proc/self/mountinfo"):
                            return rows
                        return original_read(path, *args, **kwargs)  # type: ignore[arg-type]

                    with (
                        patch.object(private_mounts, "_read_mountinfo", return_value=rows),
                        patch.object(Path, "read_text", read),
                    ):
                        with self.assertRaisesRegex(PrivateMountError, "unsupported nested mount"):
                            self.check(private)
                        with self.assertRaisesRegex(FileToolSandboxError, "nested mount"):
                            _reject_nested_mounts(private)

    def test_mount_records_use_exact_delimiters_and_tail_cardinality(self) -> None:
        row = "1 0 0:1 / /example rw - tmpfs example rw"
        for malformed in (
            "\n" + row,
            row + "\n\n",
            row + "\n\n2 1 0:1 / /another rw - tmpfs example rw",
            row.replace("1 0", "1  0"),
            row.replace("1 0", "1\t0"),
            row.replace(" - ", "  - "),
            row + " extra",
            row + " ",
        ):
            with self.subTest(malformed=repr(malformed)):
                with patch.object(private_mounts, "_read_mountinfo", return_value=malformed):
                    with self.assertRaisesRegex(PrivateMountError, "malformed"):
                        private_mounts.read_mount_table()

    def test_empty_mount_source_is_valid_but_filesystem_and_options_are_required(self) -> None:
        row = "1 0 0:1 / /example rw - tmpfs  rw\n"
        with patch.object(private_mounts, "_read_mountinfo", return_value=row):
            self.assertEqual(private_mounts.read_mount_table()[1].filesystem, "tmpfs")
        for malformed in (row.replace("tmpfs  rw", "  rw"), row.replace("tmpfs  rw", "tmpfs  ")):
            with self.subTest(malformed=malformed):
                with patch.object(private_mounts, "_read_mountinfo", return_value=malformed):
                    with self.assertRaises(PrivateMountError):
                        private_mounts.read_mount_table()

    def test_missing_private_anchor_metadata_error_is_normalized_and_closes_fds(self) -> None:
        actual = MountPins.open
        original_fstat = os.fstat
        adopted: set[int] = set()

        def track(pins: MountPins, path: Path, **kwargs: object) -> int:
            fd = actual(pins, path, **kwargs)  # type: ignore[arg-type]
            if path == self.private:
                adopted.add(fd)
            return fd

        def fail(fd: int) -> os.stat_result:
            if fd in adopted:
                raise OSError(errno.EIO, "fictional private metadata error")
            return original_fstat(fd)

        with (
            assert_descriptor_cleanup(self),
            patch.object(MountPins, "open", track),
            patch.object(os, "fstat", side_effect=fail),
        ):
            with self.assertRaisesRegex(PrivateMountError, "anchor.*unavailable") as error:
                self.check(self.private / "missing" / "key")
            self.assertIsInstance(error.exception.__cause__, OSError)

    def test_missing_private_anchor_lookup_failure_is_not_treated_as_absence(self) -> None:
        identity = self.private.stat()
        for unavailable in (False, True):

            def flags(fd: int, *_args: object) -> bytes:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                    if unavailable:
                        raise OSError(errno.ENOENT, "fictional missing lookup evidence")
                    return struct.pack("=I", 0x40000000)
                return struct.pack("=I", 0)

            with self.subTest(unavailable=unavailable), assert_descriptor_cleanup(self):
                with (
                    patch("hermes_codex_router.mount_lookup._filesystem_type", return_value=0xEF53),
                    patch("fcntl.ioctl", side_effect=flags),
                    self.assertRaisesRegex(PrivateMountError, "cannot be pinned") as error,
                ):
                    self.check(self.private / "missing" / "key")
                cause = error.exception.__cause__
                self.assertIsInstance(cause, MountPinError)
                assert isinstance(cause, MountPinError)
                self.assertIsInstance(cause.__cause__, LookupEvidenceError)

    def test_partial_private_pin_failure_closes_prior_validation_handles(self) -> None:
        (self.private / "file").write_text("fictional material", encoding="utf-8")
        with assert_descriptor_cleanup(self):
            with MountPins() as pins, PrivateMountGuard() as guard:
                fd = pins.open(self.source)
                with self.assertRaisesRegex(PrivateMountError, "cannot be pinned"):
                    guard.check(
                        {self.source: fd},
                        {self.source: pins.mount_id(fd)},
                        (self.private, self.private / "file" / "impossible"),
                    )

    def test_permission_failure_is_not_a_missing_private_path(self) -> None:
        actual = MountPins.open

        def denied(pins: MountPins, path: Path, **kwargs: object) -> int:
            if path == self.private:
                from hermes_codex_router.claude_mount_pins import MountPinError

                raise MountPinError("fixture") from PermissionError(errno.EACCES, "fixture")
            return actual(pins, path, **kwargs)  # type: ignore[arg-type]

        with assert_descriptor_cleanup(self):
            with patch.object(MountPins, "open", denied):
                with self.assertRaisesRegex(PrivateMountError, "cannot be pinned"):
                    self.check()
