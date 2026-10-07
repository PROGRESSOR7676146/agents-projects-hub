"""Race and lifecycle checks for inode-pinned Claude mount sources."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.claude_mount_pins import MountPinError, MountPins


class ClaudeMountPinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="example-pins-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.authority = self.base / "authority"
        self.authority.mkdir()
        (self.authority / "key").write_text("fictional-key", encoding="utf-8")

    def test_replaced_source_stays_on_original_inode_and_recheck_refuses(self) -> None:
        with MountPins() as pins:
            fd = pins.open(self.project, directory=True)
            identity = os.fstat(fd).st_ino
            self.project.rename(self.base / "original")
            self.project.symlink_to(self.authority, target_is_directory=True)
            self.assertEqual(os.fstat(fd).st_ino, identity)
            self.assertFalse((Path(f"/proc/self/fd/{fd}") / "key").exists())
            with self.assertRaises(MountPinError):
                pins.recheck()
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_ancestor_replacement_and_git_alias_are_refused(self) -> None:
        with MountPins() as pins:
            project_fd = pins.open(self.project, directory=True)
            git_fd = pins.open_relative(project_fd, ".git", directory=True)
            self.assertEqual(os.fstat(git_fd).st_ino, (self.project / ".git").stat().st_ino)
            (self.project / ".git").rename(self.project / "old-git")
            (self.project / ".git").symlink_to(self.authority, target_is_directory=True)
            with self.assertRaises(MountPinError):
                pins.open_relative(project_fd, ".git", directory=True)
            with self.assertRaises(MountPinError):
                pins.recheck()

    def test_component_walk_never_follows_symlinks(self) -> None:
        (self.base / "alias").symlink_to(self.project, target_is_directory=True)
        with MountPins() as pins:
            for path in (self.base / "alias", self.base / "alias" / ".git"):
                with self.subTest(path=path), self.assertRaises(MountPinError):
                    pins.open(path, directory=True)
            with self.assertRaises(MountPinError):
                pins.open(self.project / ".." / "authority", directory=True)
            project_fd = pins.open(self.project, directory=True)
            with self.assertRaises(MountPinError):
                pins.open_relative(project_fd, "../authority", directory=True)
            self.assertFalse(os.get_inheritable(project_fd))

    def test_ancestor_swap_cannot_retarget_a_pinned_source(self) -> None:
        ancestor = self.base / "projects"
        ancestor.mkdir()
        selected = ancestor / "project"
        selected.mkdir()
        with MountPins() as pins:
            descriptor = pins.open(selected, directory=True)
            expected = os.fstat(descriptor).st_ino
            ancestor.rename(self.base / "original-projects")
            ancestor.symlink_to(self.authority, target_is_directory=True)
            self.assertEqual(os.fstat(descriptor).st_ino, expected)
            with self.assertRaises(MountPinError):
                pins.recheck()

    def test_fstat_failure_before_adoption_does_not_leak_a_descriptor(self) -> None:
        opened: list[int] = []
        actual_open = os.open

        def track(*args: object, **kwargs: object) -> int:
            descriptor = actual_open(*args, **kwargs)  # type: ignore[arg-type]
            opened.append(descriptor)
            return descriptor

        with (
            patch("os.open", side_effect=track),
            patch(
                "hermes_codex_router.claude_mount_pins._identity", side_effect=OSError("example")
            ),
            MountPins() as pins,
            self.assertRaises(MountPinError),
        ):
            pins.open(self.project, directory=True)
        for descriptor in set(opened):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_partial_failure_closes_every_opened_descriptor(self) -> None:
        opened: list[int] = []
        actual_open = os.open

        def track(*args: object, **kwargs: object) -> int:
            descriptor = actual_open(*args, **kwargs)  # type: ignore[arg-type]
            opened.append(descriptor)
            return descriptor

        with patch("os.open", side_effect=track), self.assertRaises(MountPinError):
            with MountPins() as pins:
                pins.open(self.project, directory=True)
                pins.open(self.project / "missing", directory=True)
        for descriptor in set(opened):
            with self.subTest(descriptor=descriptor), self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_close_is_idempotent_and_closed_pins_cannot_be_reused(self) -> None:
        pins = MountPins()
        fd = pins.open(self.project, directory=True)
        pins.close()
        pins.close()
        with self.assertRaises(OSError):
            os.fstat(fd)
        with self.assertRaises(MountPinError):
            pins.open(self.project, directory=True)
        with self.assertRaises(MountPinError):
            pins.recheck()

    def test_mount_id_matches_the_pinned_source(self) -> None:
        with MountPins() as pins:
            fd = pins.open(self.project, directory=True)
            mount_id = pins.mount_id(fd)
            self.assertIs(type(mount_id), int)
            self.assertGreater(mount_id, 0)
            ids = {
                int(line.split()[0])
                for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
            }
            self.assertIn(mount_id, ids)
            for raw in ("", "mnt_id:\t0\n", "mnt_id:\t1\nmnt_id:\t2\n", "mnt_id:\tx\n"):
                with patch.object(Path, "read_text", return_value=raw):
                    with self.subTest(raw=raw), self.assertRaises(MountPinError):
                        pins.mount_id(fd)

    def test_normal_git_directory_activity_does_not_replace_mount_identity(self) -> None:
        with MountPins() as pins:
            project_fd = pins.open(self.project, directory=True)
            git_fd = pins.open_relative(project_fd, ".git", directory=True)
            (self.project / "new-directory").mkdir()
            (self.project / ".git" / "objects").mkdir()
            pins.recheck()
            (self.project / "new-directory").rmdir()
            (self.project / ".git" / "objects").rmdir()
            pins.recheck()
            self.assertEqual(os.fstat(project_fd).st_ino, self.project.stat().st_ino)
            self.assertEqual(os.fstat(git_fd).st_ino, (self.project / ".git").stat().st_ino)

    def test_same_inode_on_a_replaced_mount_is_refused(self) -> None:
        with MountPins() as pins:
            descriptor = pins.open(self.project, directory=True)
            before = len(os.listdir("/proc/self/fd"))
            original = pins.mount_id(descriptor)

            def changed(fd: int) -> int:
                return original if fd == descriptor else original + 1

            with patch("hermes_codex_router.claude_mount_pins.mount_id", side_effect=changed):
                with self.assertRaisesRegex(MountPinError, "identity changed"):
                    pins.recheck()
            self.assertEqual(os.fstat(descriptor).st_ino, self.project.stat().st_ino)
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)
