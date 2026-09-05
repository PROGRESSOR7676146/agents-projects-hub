from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import unittest

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
    def make_capsule(self, base: pathlib.Path) -> pathlib.Path:
        capsule = base / "agents-projects-hub"
        generation = capsule / "versions" / ("a" * 40)
        generation.mkdir(parents=True)
        manifest = {"capsule_id": "agents-projects-hub", "max_age_days": 30}
        (generation / "manifest.json").write_text(json.dumps(manifest))
        (generation / "RUNBOOK.md").write_text("safe recovery\n")
        receipt = {
            "capsule_id": "agents-projects-hub",
            "source_git_sha": "a" * 40,
            "source_clean": True,
            "published_at": "2026-09-06T00:00:00+00:00",
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


if __name__ == "__main__":
    unittest.main()
