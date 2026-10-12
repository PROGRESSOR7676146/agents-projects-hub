"""Offline checks for the pinned, atomic tlive source package."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tlive_extension.py"
SPEC = importlib.util.spec_from_file_location("tlive_extension", SCRIPT)
assert SPEC and SPEC.loader
extension = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extension)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TliveExtensionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "package"
        (self.source / "src").mkdir(parents=True)
        (self.source / "package.json").write_text('{"name":"tlive","version":"5.3.1"}\n')
        (self.source / "src" / "old.ts").write_text("export const value = 1;\n")
        self.patch_file = root / "patch.diff"
        self.patch_file.write_text(
            "--- a/src/old.ts\n+++ b/src/old.ts\n"
            "@@ -1 +1 @@\n-export const value = 1;\n+export const value = 2;\n"
            "--- a/src/new.ts\n+++ b/src/new.ts\n"
            "@@ -0,0 +1 @@\n+export const protectedValue = true;\n"
        )
        self.manifest = {
            "package": "tlive",
            "version": "5.3.1",
            "archive_sha256": "0" * 64,
            "patch_sha256": sha(self.patch_file.read_bytes()),
            "base_files": {
                "package.json": sha((self.source / "package.json").read_bytes()),
                "src/old.ts": sha((self.source / "src" / "old.ts").read_bytes()),
            },
            "patched_files": {
                "src/old.ts": sha(b"export const value = 2;\n"),
                "src/new.ts": sha(b"export const protectedValue = true;\n"),
            },
        }
        self.manifest_file = root / "manifest.json"
        self.manifest_file.write_text(json.dumps(self.manifest))
        for name, value in (("MANIFEST", self.manifest_file), ("PATCH", self.patch_file)):
            context = patch.object(extension, name, value)
            context.start()
            self.addCleanup(context.stop)

    def test_apply_verify_and_idempotence(self) -> None:
        extension.verify_tree(self.source, self.manifest, patched=False)
        extension.apply(self.source, self.manifest)
        extension.verify_tree(self.source, self.manifest, patched=True)
        extension.apply(self.source, self.manifest)
        self.assertEqual(
            (self.source / "src" / "new.ts").read_text(), "export const protectedValue = true;\n"
        )

    def test_wrong_source_and_conflict_never_mutate(self) -> None:
        old = (self.source / "src" / "old.ts").read_bytes()
        (self.source / "src" / "new.ts").write_text("conflict\n")
        with self.assertRaises(extension.SourceMismatch):
            extension.apply(self.source, self.manifest)
        self.assertEqual((self.source / "src" / "old.ts").read_bytes(), old)
        (self.source / "src" / "new.ts").unlink()
        (self.source / "src" / "old.ts").write_text("unreviewed change\n")
        with self.assertRaises(extension.SourceMismatch):
            extension.apply(self.source, self.manifest)
        self.assertEqual((self.source / "src" / "old.ts").read_text(), "unreviewed change\n")

    def test_atomic_exchange_failure_leaves_base(self) -> None:
        with patch.object(extension, "exchange_directories", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(OSError, "unavailable"):
                extension.apply(self.source, self.manifest)
        extension.verify_tree(self.source, self.manifest, patched=False)
        self.assertFalse((self.source / "src" / "new.ts").exists())

    def test_post_exchange_failure_rolls_back(self) -> None:
        original_verify = extension.verify_tree

        def fail_after_exchange(source: Path, manifest: dict, *, patched: bool) -> None:
            if source == self.source and patched and (source / "src" / "new.ts").exists():
                raise extension.SourceMismatch("injected final check")
            original_verify(source, manifest, patched=patched)

        with patch.object(extension, "verify_tree", side_effect=fail_after_exchange):
            with self.assertRaisesRegex(extension.SourceMismatch, "injected final check"):
                extension.apply(self.source, self.manifest)
        extension.verify_tree(self.source, self.manifest, patched=False)

    def test_symlink_and_extra_source_fail_closed(self) -> None:
        original = self.source / "src" / "old.ts"
        original.unlink()
        original.symlink_to(Path(self.temp.name) / "unrelated.ts")
        with self.assertRaises(extension.SourceMismatch):
            extension.apply(self.source, self.manifest)
        original.unlink()
        original.write_text("export const value = 1;\n")
        (self.source / "src" / "extra.ts").write_text("extra\n")
        with self.assertRaises(extension.SourceMismatch):
            extension.apply(self.source, self.manifest)

    def test_patch_hash_and_archive_hash_fail_closed(self) -> None:
        self.patch_file.write_text(self.patch_file.read_text() + "# changed\n")
        with self.assertRaisesRegex(extension.SourceMismatch, "patch bytes"):
            extension.load_manifest()
        archive = Path(self.temp.name) / "release.tgz"
        archive.write_bytes(b"wrong release")
        with self.assertRaisesRegex(extension.SourceMismatch, "archive"):
            extension.verify_archive(archive, self.manifest)
        self.assertEqual((self.source / "src" / "old.ts").read_text(), "export const value = 1;\n")


class BundledPackageTest(unittest.TestCase):
    def test_patch_manifest_scope_and_attribution(self) -> None:
        package = extension.PACKAGE
        manifest = json.loads((package / "source-manifest.json").read_text())
        self.assertEqual((manifest["package"], manifest["version"]), ("tlive", "5.3.1"))
        self.assertEqual(
            sha((package / "tlive-5.3.1-protected-permissions.patch").read_bytes()),
            manifest["patch_sha256"],
        )
        self.assertEqual(
            set(manifest["patched_files"]),
            {
                "src/adapters/im/telegram.ts",
                "src/kernel/contracts/protected-permission.ts",
                "src/kernel/daemon/bootstrap.ts",
                "src/kernel/daemon/protected-permissions.ts",
                "src/kernel/ipc/protocol.ts",
            },
        )
        self.assertIn("src/kernel/daemon/permission-router.ts", manifest["base_files"])
        self.assertNotIn("src/kernel/daemon/permission-router.ts", manifest["patched_files"])
        self.assertTrue((package / "LICENSE.upstream").read_bytes().startswith(b"MIT License\n"))


if __name__ == "__main__":
    unittest.main()
