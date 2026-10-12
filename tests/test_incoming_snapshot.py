from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.incoming_snapshot import IncomingSnapshotError, read_verified_snapshot


class IncomingSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "spool"
        self.path = self.root / "aa" / "example"
        self.path.parent.mkdir(parents=True)
        self.data = b"fictional-immutable-bytes"
        self.path.write_bytes(self.data)
        self.digest = hashlib.sha256(self.data).hexdigest()

    def read(self, **options: object) -> bytes:
        return read_verified_snapshot(
            self.root,
            self.path,
            expected_size=options.pop("size", len(self.data)),  # type: ignore[arg-type]
            expected_sha256=options.pop("digest", self.digest),  # type: ignore[arg-type]
            max_bytes=options.pop("limit", 100),  # type: ignore[arg-type]
        )

    def test_reads_immutable_exact_bytes_without_path_reopen(self) -> None:
        with patch.object(Path, "read_bytes", side_effect=AssertionError("no path reopen")):
            result = self.read()
        self.assertEqual(result, self.data)
        self.path.write_bytes(b"changed after snapshot")
        self.assertEqual(result, self.data)

    def test_replaced_path_after_open_cannot_change_snapshot(self) -> None:
        original = os.read
        replaced = False

        def replacing(fd: int, size: int) -> bytes:
            nonlocal replaced
            if not replaced:
                replaced = True
                replacement = self.path.with_name("replacement")
                replacement.write_bytes(b"different bytes")
                os.replace(replacement, self.path)
            return original(fd, size)

        with patch("hermes_codex_router.incoming_snapshot.os.read", side_effect=replacing):
            # Unlink changes the pinned inode's ctime: conservative refusal is safe.
            with self.assertRaises(IncomingSnapshotError):
                self.read()

    def test_rejects_outside_paths_and_every_symlink_component(self) -> None:
        target = self.root.parent / "outside"
        target.write_bytes(self.data)
        variants = (self.root / "linked-file", self.root / "linked-dir" / "outside")
        variants[0].symlink_to(target)
        (self.root / "linked-dir").symlink_to(self.root.parent, target_is_directory=True)
        for path in (target, *variants, self.root / "aa" / ".." / "aa" / "example"):
            with self.subTest(path=path.name), self.assertRaises(IncomingSnapshotError):
                read_verified_snapshot(
                    self.root,
                    path,
                    expected_size=len(self.data),
                    expected_sha256=self.digest,
                    max_bytes=100,
                )
        original = self.root
        link = self.root.parent / "root-link"
        link.symlink_to(original, target_is_directory=True)
        with self.assertRaises(IncomingSnapshotError):
            read_verified_snapshot(
                link,
                link / "aa" / "example",
                expected_size=len(self.data),
                expected_sha256=self.digest,
                max_bytes=100,
            )

    def test_rejects_nonregular_fifo_without_blocking(self) -> None:
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(IncomingSnapshotError):
            self.read()

    def test_checks_metadata_before_read_and_detects_tampering(self) -> None:
        for options in (
            {"size": True},
            {"size": -1},
            {"size": 101},
            {"size": 1},
            {"limit": True},
            {"digest": "bad"},
        ):
            with self.subTest(options=options), self.assertRaises(IncomingSnapshotError):
                self.read(**options)
        with patch("hermes_codex_router.incoming_snapshot.os.read") as read:
            with self.assertRaises(IncomingSnapshotError):
                self.read(limit=2)
            read.assert_not_called()
        self.path.write_bytes(b"x" * len(self.data))
        with self.assertRaises(IncomingSnapshotError):
            self.read()

    def test_short_reads_and_interrupted_read_preserve_bytes(self) -> None:
        original = os.read
        calls = 0

        def short(fd: int, size: int) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InterruptedError()
            return original(fd, min(size, 3))

        with patch("hermes_codex_router.incoming_snapshot.os.read", side_effect=short):
            self.assertEqual(self.read(), self.data)
        self.assertGreater(calls, 3)


if __name__ == "__main__":
    unittest.main()
