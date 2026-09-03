from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.artifacts import (
    ArtifactSecurityError,
    collect_staged_artifacts,
    remove_spooled_artifact,
    spool_staged_artifacts,
    validate_artifact_path,
    verify_spooled_artifact,
)


class ArtifactValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_markdown_artifact(self) -> None:
        doc = self.project_root / "report.md"
        doc.write_text("# Report\nContent", encoding="utf-8")

        artifact = validate_artifact_path(doc, self.project_root)
        self.assertEqual(artifact.name, "report.md")
        self.assertEqual(artifact.path, doc)
        self.assertGreater(artifact.size, 0)
        self.assertEqual(artifact.mime_type, "text/markdown")

    def test_rejects_escaping_project_root(self) -> None:
        outside = self.project_root.parent / "escape.md"
        outside.write_text("Outside", encoding="utf-8")
        try:
            with self.assertRaises(ArtifactSecurityError) as ctx:
                validate_artifact_path(outside, self.project_root)
            self.assertIn("escapes project root", str(ctx.exception))
        finally:
            outside.unlink(missing_ok=True)

    def test_rejects_symlink(self) -> None:
        target = self.project_root / "target.txt"
        target.write_text("hello", encoding="utf-8")
        symlink = self.project_root / "symlink.txt"
        symlink.symlink_to(target)

        with self.assertRaises(ArtifactSecurityError) as ctx:
            validate_artifact_path(symlink, self.project_root)
        self.assertIn("regular non-symlink", str(ctx.exception))

    def test_rejects_sensitive_names(self) -> None:
        for bad_name in ["my_api_key.txt", "user_secret.json", "auth_token.md", "id_rsa.txt"]:
            bad_file = self.project_root / bad_name
            bad_file.write_text("data", encoding="utf-8")
            with self.assertRaises(ArtifactSecurityError):
                validate_artifact_path(bad_file, self.project_root)

    def test_rejects_forbidden_extensions(self) -> None:
        for ext in [".pem", ".sh", ".pyc", ".db", ".exe", ".so"]:
            bad_file = self.project_root / f"file{ext}"
            bad_file.write_text("data", encoding="utf-8")
            with self.assertRaises(ArtifactSecurityError):
                validate_artifact_path(bad_file, self.project_root)

    def test_rejects_empty_file(self) -> None:
        empty = self.project_root / "empty.md"
        empty.write_text("", encoding="utf-8")
        with self.assertRaises(ArtifactSecurityError) as ctx:
            validate_artifact_path(empty, self.project_root)
        self.assertIn("empty", str(ctx.exception))

    def test_collect_staged_artifacts_uses_only_current_job(self) -> None:
        job_id = "job-42"
        job_staging = self.project_root / ".hub" / "staging" / job_id
        job_staging.mkdir(parents=True, exist_ok=True)
        doc1 = job_staging / "spec.json"
        doc1.write_text('{"name": "test"}', encoding="utf-8")

        general_staging = self.project_root / ".hub" / "staging"
        doc2 = general_staging / "summary.md"
        doc2.write_text("# Summary", encoding="utf-8")

        artifacts = collect_staged_artifacts(self.project_root, job_id)
        self.assertEqual([a.name for a in artifacts], ["spec.json"])

    def test_collect_staged_artifacts_does_not_drop_files_by_count(self) -> None:
        job_id = "job-bound"
        job_staging = self.project_root / ".hub" / "staging" / job_id
        job_staging.mkdir(parents=True, exist_ok=True)

        for i in range(15):
            (job_staging / f"doc_{i:02d}.txt").write_text(f"doc {i}", encoding="utf-8")

        artifacts = collect_staged_artifacts(self.project_root, job_id)
        self.assertEqual(len(artifacts), 15)

    def test_collect_staged_artifacts_bounds_total_bytes(self) -> None:
        job_id = "job-total-size"
        staging = self.project_root / ".hub" / "staging" / job_id
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "one.txt").write_text("123456", encoding="utf-8")
        (staging / "two.txt").write_text("123456", encoding="utf-8")
        rejected: list[str] = []

        artifacts = collect_staged_artifacts(
            self.project_root, job_id, max_total_bytes=10, rejection_sink=rejected
        )

        self.assertEqual([item.name for item in artifacts], ["one.txt"])
        self.assertEqual(rejected, ["two.txt: total attachment size limit exceeded"])

    def test_rejects_archives_and_unsafe_multipart_filename(self) -> None:
        for name in ["bundle.zip", 'report".md', "report\r\nInjected.md"]:
            candidate = self.project_root / name
            candidate.write_text("data", encoding="utf-8")
            with self.assertRaises(ArtifactSecurityError):
                validate_artifact_path(candidate, self.project_root)

    def test_spool_is_private_immutable_snapshot_and_can_be_removed(self) -> None:
        job_id = "job-snapshot"
        staging = self.project_root / ".hub" / "staging" / job_id
        staging.mkdir(parents=True)
        source = staging / "report.md"
        source.write_text("original", encoding="utf-8")
        spool = self.project_root.parent / f"spool-{self.project_root.name}"
        try:
            artifacts = spool_staged_artifacts(self.project_root, job_id, spool)
            self.assertEqual(len(artifacts), 1)
            snapshot = artifacts[0]
            self.assertEqual(snapshot.path.read_text(encoding="utf-8"), "original")
            self.assertEqual(snapshot.path.stat().st_mode & 0o777, 0o600)
            source.write_text("changed", encoding="utf-8")
            verify_spooled_artifact(
                snapshot.path,
                spool,
                expected_size=snapshot.size,
                expected_sha256=snapshot.sha256,
            )
            remove_spooled_artifact(snapshot.path, spool)
            self.assertFalse(snapshot.path.exists())
        finally:
            if spool.exists():
                for path in sorted(spool.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    else:
                        path.rmdir()
                spool.rmdir()

    def test_spool_verification_rejects_replacement(self) -> None:
        job_id = "job-replaced"
        staging = self.project_root / ".hub" / "staging" / job_id
        staging.mkdir(parents=True)
        (staging / "report.md").write_text("safe", encoding="utf-8")
        spool = self.project_root.parent / f"spool-{self.project_root.name}"
        try:
            snapshot = spool_staged_artifacts(self.project_root, job_id, spool)[0]
            snapshot.path.write_text("evil", encoding="utf-8")
            with self.assertRaises(ArtifactSecurityError):
                verify_spooled_artifact(
                    snapshot.path,
                    spool,
                    expected_size=snapshot.size,
                    expected_sha256=snapshot.sha256,
                )
        finally:
            if spool.exists():
                for path in sorted(spool.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    else:
                        path.rmdir()
                spool.rmdir()


if __name__ == "__main__":
    unittest.main()
