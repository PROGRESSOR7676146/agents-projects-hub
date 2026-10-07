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

import hermes_codex_router.review_materials as materials
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

    def test_invalid_selection_names_limits_and_bindings_refuse_before_file_reads(self) -> None:
        selected = self.selection()
        for name in ("../visible.txt", "/visible.txt", "a/./b", "a//b", ".git/HEAD", "a\\b"):
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
        with assert_descriptor_cleanup(self):
            with patch.object(materials.os, "write", side_effect=OSError("fictional fault")):
                with self.assertRaises(ReviewMaterialError):
                    build_review_capsule(self.root, (self.selection(),), binding="example-result")

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
            with patch.object(
                Path, "read_text", return_value="1 2 0:1 / / rw - unknown example rw\n"
            ):
                with self.assertRaises(ReviewMaterialError):
                    build_review_capsule(self.root, (selected,), binding="example-result")
            with patch.object(
                materials.fcntl, "fcntl", side_effect=OSError("fictional seal fault")
            ):
                with self.assertRaises(ReviewMaterialError):
                    build_review_capsule(self.root, (selected,), binding="example-result")

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
