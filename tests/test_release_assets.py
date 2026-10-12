from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.release_assets import seal_assets
from tests.test_deployment_manifest import _wheel


class ReleaseAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "source"
        self.output = self.base / "assets"
        self.root.mkdir()
        self.output.mkdir()
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "agents-projects-hub"\nversion = "0.7.1"\n'
        )
        for name in ("requirements-release.lock", "requirements-build.lock", "uv.lock"):
            (self.root / name).write_text("fictional locked dependencies\n")
        self.wheel = _wheel(
            self.output / "agents_projects_hub-0.7.1-py3-none-any.whl",
            version="0.7.1",
            git_sha="a" * 40,
            schema_max=41,
        )
        self.archive = self.output / "agents_projects_hub-0.7.1.tar.gz"
        import tarfile

        with tarfile.open(self.archive, "w:gz") as archive:
            content = (self.root / "pyproject.toml").read_bytes()
            info = tarfile.TarInfo("agents_projects_hub-0.7.1/pyproject.toml")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    def test_sealed_assets_bind_clean_wheel_and_exact_locks_without_local_paths(self) -> None:
        report = seal_assets(self.root, self.output, "a" * 40)
        self.assertEqual(report["git_sha"], "a" * 40)
        self.assertEqual(report["schema_max"], 41)
        self.assertNotIn(str(self.base), json.dumps(report))
        for name in ("requirements-release.lock", "requirements-build.lock", "uv.lock"):
            self.assertEqual((self.output / name).read_bytes(), (self.root / name).read_bytes())
        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        self.assertEqual(len((self.output / "SHA256SUMS").read_text().splitlines()), 6)

    def test_refuses_wheel_from_another_commit_before_publishing_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "revision"):
            seal_assets(self.root, self.output, "b" * 40)
        self.assertFalse((self.output / "SHA256SUMS").exists())

    def test_refuses_dirty_wheel_before_publishing_metadata(self) -> None:
        _wheel(self.wheel, version="0.7.1", git_sha="a" * 40, schema_max=41, clean=False)
        with self.assertRaisesRegex(RuntimeError, "clean"):
            seal_assets(self.root, self.output, "a" * 40)
        self.assertFalse((self.output / "SHA256SUMS").exists())

    def test_refuses_wrong_package_version(self) -> None:
        _wheel(self.wheel, version="0.7.0", git_sha="a" * 40, schema_max=41)
        with self.assertRaisesRegex(ValueError, "version"):
            seal_assets(self.root, self.output, "a" * 40)

    def test_refuses_contaminated_output(self) -> None:
        (self.output / "unrelated.txt").write_text("not a release asset")
        with self.assertRaisesRegex(ValueError, "asset set"):
            seal_assets(self.root, self.output, "a" * 40)

    def test_refuses_archive_path_escape_or_private_directory(self) -> None:
        import tarfile

        for name in ("../escape", "/absolute", "agents_projects_hub-0.7.1/.hub/private"):
            with self.subTest(name=name):
                with tarfile.open(self.archive, "w:gz") as archive:
                    info = tarfile.TarInfo(name)
                    archive.addfile(info, io.BytesIO())
                with self.assertRaisesRegex(ValueError, "source archive"):
                    seal_assets(self.root, self.output, "a" * 40)

    def test_refuses_missing_or_mismatched_archived_package_metadata(self) -> None:
        import tarfile

        for content in (None, b'[project]\nversion = "0.6.0"\n'):
            with self.subTest(content=content):
                with tarfile.open(self.archive, "w:gz") as archive:
                    if content is not None:
                        info = tarfile.TarInfo("agents_projects_hub-0.7.1/pyproject.toml")
                        info.size = len(content)
                        archive.addfile(info, io.BytesIO(content))
                with self.assertRaisesRegex(ValueError, "source archive metadata"):
                    seal_assets(self.root, self.output, "a" * 40)
                self.assertFalse((self.output / "SHA256SUMS").exists())

    def test_unchanged_tree_and_commit_required_after_build(self) -> None:
        from scripts.release_assets import build_assets

        with patch("scripts.release_assets.clean_revision", side_effect=["a" * 40, "b" * 40]):
            with patch("scripts.release_assets.subprocess.run"):
                with self.assertRaisesRegex(ValueError, "changed"):
                    build_assets(self.root, self.base / "new-assets")

    def test_build_backend_is_hash_constrained_and_archive_uses_exact_commit(self) -> None:
        from scripts.release_assets import build_assets

        with patch("scripts.release_assets.clean_revision", return_value="a" * 40):
            with patch("scripts.release_assets.subprocess.run") as run:
                with patch("scripts.release_assets.seal_assets", return_value={}):
                    build_assets(self.root, self.base / "new-assets")
        self.assertEqual(run.call_count, 2)
        args = run.call_args_list[0].args[0]
        self.assertIn("--require-hashes", args)
        self.assertIn("--no-build-logs", args)
        self.assertEqual(
            args[args.index("--build-constraints") + 1],
            str(self.root / "requirements-build.lock"),
        )
        archive_args = run.call_args_list[1].args[0]
        self.assertEqual(archive_args[:2], ["git", "archive"])
        self.assertEqual(archive_args[-1], "a" * 40)

    def test_refuses_ignored_stale_build_products(self) -> None:
        from scripts.release_assets import build_assets

        (self.root / "build").mkdir()
        with patch("scripts.release_assets.clean_revision", return_value="a" * 40):
            with patch("scripts.release_assets.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "fresh checkout"):
                    build_assets(self.root, self.base / "new-assets")
                run.assert_not_called()
