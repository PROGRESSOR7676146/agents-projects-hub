"""Caller-selected materials are captured as sealed bytes, never an authorization grant."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.claude_private_mounts as private_mounts
import hermes_codex_router.review_materials as materials
from hermes_codex_router.claude_mount_pins import MountPins, mount_id
from hermes_codex_router.review_materials import (
    MaterialSelection,
    ReviewMaterialError,
    build_review_capsule,
    decode_review_capsule,
)
from tests.fd_fixture import assert_descriptor_cleanup


class ReviewMaterialsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-review-materials-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        self.file = self.root / "visible.txt"
        self.file.write_text("authorized text", encoding="utf-8")

    def selection(self, name: str = "visible.txt") -> MaterialSelection:
        data = (self.root / name).read_bytes()
        return MaterialSelection(name, len(data), hashlib.sha256(data).hexdigest())

    def test_capture_is_deterministic_sealed_and_independent_of_later_source_changes(self) -> None:
        selection = self.selection()
        with assert_descriptor_cleanup(self):
            with build_review_capsule(self.root, (selection,), binding="example-result") as capsule:
                original = capsule.read()
                manifest = decode_review_capsule(original, capsule.digest)
                self.assertEqual(manifest["files"][0]["text"], "authorized text")
                self.assertNotIn(str(self.root), original.decode())
                with self.assertRaises(OSError):
                    os.pwrite(capsule.fileno(), b"x", 0)
                with self.assertRaises(OSError):
                    os.ftruncate(capsule.fileno(), 0)
                with self.assertRaises(OSError):
                    os.ftruncate(capsule.fileno(), capsule.size + 1)
                with self.assertRaises(OSError):
                    fcntl.fcntl(capsule.fileno(), materials._F_ADD_SEALS, 0x0001)
                self.assertFalse(os.get_inheritable(capsule.fileno()))
                self.file.write_text("later source change", encoding="utf-8")
                self.assertEqual(capsule.read(), original)
        self.file.write_text("authorized text", encoding="utf-8")
        with build_review_capsule(self.root, (selection,), binding="example-result") as again:
            self.assertEqual(again.read(), original)

    def test_selected_files_require_exact_size_digest_and_utf8(self) -> None:
        selected = self.selection()
        for item in (
            MaterialSelection(selected.name, selected.size + 1, selected.sha256),
            MaterialSelection(selected.name, selected.size, "0" * 64),
        ):
            with self.subTest(item=item), self.assertRaises(ReviewMaterialError):
                build_review_capsule(self.root, (item,), binding="example-result")
        for data in (b"\xff", b"contains\x00nul"):
            self.file.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(ReviewMaterialError):
                build_review_capsule(self.root, (self.selection(),), binding="example-result")

    def test_scripted_file_alias_refuses_before_content_read(self) -> None:
        actual = self.selection()
        selected = MaterialSelection("Visible.txt", actual.size, actual.sha256)
        open_relative = MountPins.open_relative

        def casefold_open(pins: MountPins, parent: int, name: str, **kwargs: object) -> int:
            # Scripted name divergence, not casefold/cache-state evidence.
            self.assertEqual(name, "Visible.txt")
            return open_relative(pins, parent, "visible.txt", directory=False)

        with assert_descriptor_cleanup(self):
            with (
                patch.object(MountPins, "open_relative", casefold_open),
                patch.object(materials, "_read_selected", wraps=materials._read_selected) as read,
            ):
                with self.assertRaisesRegex(ReviewMaterialError, "descriptor path differs"):
                    with build_review_capsule(self.root, (selected,), binding="example-result"):
                        pass
                read.assert_not_called()

    def test_root_and_selected_descriptor_aliases_or_unavailable_paths_refuse(self) -> None:
        selected = self.selection()
        readlink = os.readlink
        for target in (self.root, self.file):
            for unavailable in (False, True):
                with self.subTest(target=target.name, unavailable=unavailable):

                    def alias(path: str) -> str:
                        actual = readlink(path)
                        if actual != str(target):
                            return actual
                        if unavailable:
                            raise OSError("fictional kernel path failure")
                        return actual + "-alias"

                    with assert_descriptor_cleanup(self):
                        with (
                            patch.object(materials.os, "readlink", side_effect=alias),
                            patch.object(
                                materials, "_read_selected", wraps=materials._read_selected
                            ) as read,
                        ):
                            with self.assertRaises(ReviewMaterialError) as raised:
                                with build_review_capsule(
                                    self.root, (selected,), binding="example-result"
                                ):
                                    pass
                            if unavailable:
                                self.assertIsInstance(raised.exception.__cause__, OSError)
                            else:
                                self.assertIn("descriptor path differs", str(raised.exception))
                            read.assert_not_called()

    def test_descriptor_paths_are_rechecked_after_capture_before_sealing(self) -> None:
        selected = self.selection()
        readlink, read = os.readlink, materials._read_selected
        for target in (self.root, self.file):
            with self.subTest(target=target.name):
                captured = False

                def capture(fd: int, size: int) -> bytes:
                    nonlocal captured
                    data = read(fd, size)
                    captured = True
                    return data

                def alias(path: str) -> str:
                    actual = readlink(path)
                    return actual + "-alias" if captured and actual == str(target) else actual

                with assert_descriptor_cleanup(self):
                    with (
                        patch.object(materials, "_read_selected", side_effect=capture),
                        patch.object(materials.os, "readlink", side_effect=alias),
                        patch.object(
                            materials,
                            "_create_sealable_memfd",
                            wraps=materials._create_sealable_memfd,
                        ) as create,
                    ):
                        with self.assertRaisesRegex(ReviewMaterialError, "descriptor path differs"):
                            with build_review_capsule(
                                self.root, (selected,), binding="example-result"
                            ):
                                pass
                        self.assertTrue(captured)
                        create.assert_not_called()

    def test_root_inside_git_metadata_refuses_before_content_read(self) -> None:
        for component in (".git", ".GIT"):
            root = self.root / component / "nested"
            root.mkdir(parents=True)
            with patch.object(materials, "_read_selected") as read:
                with self.assertRaisesRegex(ReviewMaterialError, "root includes Git metadata"):
                    build_review_capsule(root, (self.selection(),), binding="example-result")
                read.assert_not_called()

    def test_final_pin_recheck_rejects_earlier_source_replaced_during_later_read(self) -> None:
        (self.root / "z-trigger.txt").write_text("later authorized text", encoding="utf-8")
        selection = (self.selection(), self.selection("z-trigger.txt"))
        read = materials._read_selected
        reads = 0

        def replace_earlier(fd: int, size: int) -> bytes:
            nonlocal reads
            data = read(fd, size)
            reads += 1
            if reads == 2:
                self.file.rename(self.root / "old-visible")
                self.file.write_text("authorized text", encoding="utf-8")
            return data

        with assert_descriptor_cleanup(self):
            with patch.object(materials, "_read_selected", side_effect=replace_earlier):
                with self.assertRaises(ReviewMaterialError) as raised:
                    build_review_capsule(self.root, selection, binding="example-result")
                self.assertEqual(reads, 2)
                self.assertIsInstance(raised.exception.__cause__, materials.MountPinError)
                self.assertEqual(str(raised.exception.__cause__), "mount source identity changed")

    def test_encoded_bound_can_refuse_bounded_source_before_memfd_creation(self) -> None:
        selection = []
        for index in range(4):
            name = f"visible-{index}.txt"
            (self.root / name).write_bytes(b"\x01" * (64 * 1024))
            selection.append(self.selection(name))
        with assert_descriptor_cleanup(self):
            with patch.object(materials, "_create_sealable_memfd") as create:
                with self.assertRaisesRegex(ReviewMaterialError, "encoding exceeds its bound"):
                    build_review_capsule(self.root, selection, binding="example-result")
                create.assert_not_called()

    def test_invalid_selection_names_limits_and_bindings_refuse_before_file_reads(self) -> None:
        selected = self.selection()
        for name in (
            "../visible.txt",
            "/visible.txt",
            "a/./b",
            "a//b",
            ".git/HEAD",
            ".GIT/config",
            "a\\b",
        ):
            with self.subTest(name=name), self.assertRaises(ReviewMaterialError):
                build_review_capsule(
                    self.root, (MaterialSelection(name, 0, "0" * 64),), binding="example-result"
                )
        for selection in ((), (selected, selected), (selected,) * 33):
            with self.subTest(count=len(selection)), self.assertRaises(ReviewMaterialError):
                build_review_capsule(self.root, selection, binding="example-result")
        for binding in ("", "contains a path /", "x" * 129):
            with self.subTest(binding=binding), self.assertRaises(ReviewMaterialError):
                build_review_capsule(self.root, (selected,), binding=binding)
        self.file.write_bytes(b"x" * (64 * 1024 + 1))
        with self.assertRaises(ReviewMaterialError):
            build_review_capsule(self.root, (self.selection(),), binding="example-result")

    def test_symlink_hardlink_special_file_and_replaced_parent_refuse(self) -> None:
        (self.root / "link").symlink_to(self.file)
        with self.assertRaises(ReviewMaterialError):
            build_review_capsule(
                self.root, (MaterialSelection("link", 15, "0" * 64),), binding="example-result"
            )
        os.link(self.file, self.root / "hardlink")
        with self.assertRaises(ReviewMaterialError):
            build_review_capsule(self.root, (self.selection(),), binding="example-result")
        (self.root / "hardlink").unlink()
        os.mkfifo(self.root / "fifo")
        with self.assertRaises(ReviewMaterialError):
            build_review_capsule(
                self.root, (MaterialSelection("fifo", 0, "0" * 64),), binding="example-result"
            )
        selected = self.selection()
        read = materials._read_selected
        with assert_descriptor_cleanup(self):

            def replace_source(fd: int, size: int) -> bytes:
                data = read(fd, size)
                self.file.rename(self.root / "old-visible")
                self.file.write_bytes(data)
                return data

            with patch.object(materials, "_read_selected", side_effect=replace_source):
                with self.assertRaises(ReviewMaterialError):
                    build_review_capsule(self.root, (selected,), binding="example-result")

    def test_decoder_rejects_corruption_duplicate_keys_and_unbound_input(self) -> None:
        with build_review_capsule(
            self.root, (self.selection(),), binding="example-result"
        ) as capsule:
            raw = capsule.read()
            with self.assertRaises(ReviewMaterialError):
                decode_review_capsule(raw, "0" * 64)
            for mutated in (
                raw + b"x",
                b'{"version":1,"version":1}',
                b"[]",
                b'{"version":' + b"1" * 5000 + b"}",
                b"x" * (1024 * 1024 + 1),
            ):
                with self.subTest(length=len(mutated)), self.assertRaises(ReviewMaterialError):
                    decode_review_capsule(mutated, hashlib.sha256(mutated).hexdigest())
            data = json.loads(raw)
            data["files"][0]["text"] = "unauthorized changed content"
            changed = json.dumps(data).encode()
            with self.assertRaises(ReviewMaterialError):
                decode_review_capsule(changed, hashlib.sha256(changed).hexdigest())

    def test_memfd_write_fault_closes_every_owned_descriptor(self) -> None:
        created: list[int] = []
        allocate = materials._create_sealable_memfd

        def create() -> int:
            fd = allocate()
            created.append(fd)
            return fd

        with assert_descriptor_cleanup(self):
            with (
                patch.object(materials, "_create_sealable_memfd", side_effect=create),
                patch.object(materials.os, "write", side_effect=OSError("fictional fault")),
            ):
                with self.assertRaises(ReviewMaterialError):
                    build_review_capsule(self.root, (self.selection(),), binding="example-result")
            self.assertEqual(len(created), 1)
            with self.assertRaises(OSError):
                os.fstat(created[0])

    def test_unsupported_mount_seal_failure_and_in_capture_mutation_refuse(self) -> None:
        selected = self.selection()
        read = materials._read_selected

        def mutate(fd: int, size: int) -> bytes:
            captured = read(fd, size)
            self.file.write_text("changed during capture", encoding="utf-8")
            return captured

        with assert_descriptor_cleanup(self):
            with patch.object(materials, "_read_selected", side_effect=mutate):
                with self.assertRaises(ReviewMaterialError):
                    build_review_capsule(self.root, (selected,), binding="example-result")
            self.file.write_text("authorized text", encoding="utf-8")
            with MountPins() as pins:
                root_fd = pins.open(self.root, directory=True)
                identity = mount_id(root_fd)
            rows = private_mounts._read_mountinfo().split("\n")
            for index, row in enumerate(rows):
                before, separator, after = row.partition(" - ")
                if before.split(" ", 1)[0] == str(identity):
                    self.assertEqual(separator, " - ")
                    rows[index] = before + separator + "unknown " + after.split(" ", 1)[1]
                    break
            else:
                self.fail("fixture root mount missing from kernel table")
            table = "\n".join(rows)
            with (
                patch.object(private_mounts, "_read_mountinfo", return_value=table) as evidence,
                patch.object(materials, "_read_selected") as source_read,
            ):
                with self.assertRaises(ReviewMaterialError) as raised:
                    build_review_capsule(self.root, (selected,), binding="example-result")
                self.assertIsInstance(raised.exception.__cause__, materials.NamespaceError)
                self.assertEqual(
                    str(raised.exception.__cause__),
                    "writable roots require a supported native Linux filesystem",
                )
                evidence.assert_called_once()
                source_read.assert_not_called()
            with patch.object(
                materials.fcntl, "fcntl", side_effect=OSError("fictional seal fault")
            ):
                allocate = materials._create_sealable_memfd
                created: list[int] = []

                def create() -> int:
                    fd = allocate()
                    created.append(fd)
                    return fd

                with patch.object(materials, "_create_sealable_memfd", side_effect=create):
                    with self.assertRaises(ReviewMaterialError):
                        build_review_capsule(self.root, (selected,), binding="example-result")
                self.assertEqual(len(created), 1)
                with self.assertRaises(OSError):
                    os.fstat(created[0])

    def test_aggregate_bound_and_empty_text_are_distinct_from_empty_selection(self) -> None:
        selected = []
        for index in range(5):
            name = f"visible-{index}.txt"
            (self.root / name).write_bytes(b"x" * (64 * 1024))
            selected.append(self.selection(name))
        with self.assertRaises(ReviewMaterialError):
            build_review_capsule(self.root, selected, binding="example-result")
        self.file.write_bytes(b"")
        with build_review_capsule(
            self.root, (self.selection(),), binding="example-result"
        ) as capsule:
            self.assertEqual(
                decode_review_capsule(capsule.read(), capsule.digest)["files"][0]["text"], ""
            )
