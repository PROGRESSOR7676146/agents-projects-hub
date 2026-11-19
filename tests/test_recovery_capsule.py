from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO

from hermes_codex_router.schema_compatibility import TARGET_SCHEMA_VERSION

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


VERIFY = load("verify_capsule", ROOT / "scripts/verify-recovery-capsule.py")


class RecoveryCapsuleTests(unittest.TestCase):
    def test_repository_capsule_matches_current_target_schema(self) -> None:
        source = ROOT / "recovery/agents-projects-hub"
        manifest = json.loads((source / "manifest.json").read_text())
        runbook = (source / "RUNBOOK.md").read_text()
        with (ROOT / "pyproject.toml").open("rb") as stream:
            package_version = str(tomllib.load(stream)["project"]["version"])
        self.assertEqual(manifest["maintained_release"], package_version)
        self.assertIn(f"Hub `{package_version}`", runbook)
        self.assertEqual(manifest["state_schema"], TARGET_SCHEMA_VERSION)
        self.assertIn(f"state schema `{TARGET_SCHEMA_VERSION}`", runbook)
        self.assertIn(f"schema `{TARGET_SCHEMA_VERSION}/{TARGET_SCHEMA_VERSION}`", runbook)

    def make_capsule(
        self, base: pathlib.Path, *, published_at: str = "2026-09-06T00:00:00+00:00"
    ) -> pathlib.Path:
        capsule = base / "agents-projects-hub"
        generation = capsule / "versions" / ("a" * 40)
        generation.mkdir(parents=True)
        manifest = {
            "capsule_id": "agents-projects-hub",
            "max_age_days": 30,
            "maintained_release": "0.11.27",
            "state_schema": 59,
        }
        (generation / "manifest.json").write_text(json.dumps(manifest))
        (generation / "RUNBOOK.md").write_text("safe recovery\n")
        receipt = {
            "capsule_id": "agents-projects-hub",
            "source_git_sha": "a" * 40,
            "source_clean": True,
            "maintained_release": "0.11.27",
            "state_schema": 59,
            "published_at": published_at,
            "files": {
                name: VERIFY.digest(generation / name) for name in ("manifest.json", "RUNBOOK.md")
            },
        }
        (generation / "receipt.json").write_text(json.dumps(receipt))
        (capsule / "current").symlink_to(pathlib.Path("versions") / ("a" * 40))
        return generation

    def test_valid_and_tampered_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            generation = self.make_capsule(base)
            now = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)
            self.assertEqual([], VERIFY.verify(base, "agents-projects-hub", now))
            (generation / "RUNBOOK.md").write_text("tampered\n")
            self.assertIn(
                "content hash mismatch: RUNBOOK.md", VERIFY.verify(base, "agents-projects-hub", now)
            )

    def test_rejects_stale_dirty_and_escaping_capsules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            generation = self.make_capsule(base)
            receipt_path = generation / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["source_clean"] = False
            receipt_path.write_text(json.dumps(receipt))
            now = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
            errors = VERIFY.verify(base, "agents-projects-hub", now)
            self.assertIn("capsule was not published from a clean source tree", errors)
            self.assertIn("capsule is stale", errors)
            (base / "agents-projects-hub/current").unlink()
            (base / "agents-projects-hub/current").symlink_to(base)
            self.assertIn("escapes", VERIFY.verify(base, "agents-projects-hub", now)[0])

    def test_expected_identity_rejects_a_fresh_capsule_for_another_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            self.make_capsule(base)
            now = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)
            self.assertEqual(
                [],
                VERIFY.verify(
                    base,
                    "agents-projects-hub",
                    now,
                    expected_source_git_sha="a" * 40,
                    expected_package_version="0.11.27",
                    expected_state_schema=59,
                ),
            )
            errors = VERIFY.verify(
                base,
                "agents-projects-hub",
                now,
                expected_source_git_sha="b" * 40,
                expected_package_version="0.11.28",
                expected_state_schema=59,
            )
            self.assertIn(
                "capsule source revision does not match expected release identity", errors
            )
            self.assertIn(
                "capsule package release does not match expected release identity", errors
            )
            self.assertEqual(
                VERIFY.verify(
                    base,
                    "agents-projects-hub",
                    now,
                    expected_source_git_sha="a" * 40,
                ),
                [
                    "expected recovery capsule identity must include source SHA, package release, and state schema"
                ],
            )

    def test_cli_distinguishes_generic_from_release_bound_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            self.make_capsule(base, published_at=dt.datetime.now(dt.timezone.utc).isoformat())
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    VERIFY.main(["--root", str(base), "agents-projects-hub"]),
                    0,
                )
            self.assertIn("deployment compatibility was not checked", output.getvalue())
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    VERIFY.main(
                        [
                            "--root",
                            str(base),
                            "agents-projects-hub",
                            "--expected-source-git-sha",
                            "a" * 40,
                            "--expected-package-version",
                            "0.11.27",
                            "--expected-state-schema",
                            "59",
                        ]
                    ),
                    0,
                )
            self.assertIn("expected release identity", output.getvalue())


if __name__ == "__main__":
    unittest.main()
