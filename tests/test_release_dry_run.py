from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from hermes_codex_router.release_dry_run import report_dict, run_release_dry_run
from tests.test_deployment_manifest import _wheel


def _runnable_wheel(path: Path, *, version: str, git_sha: str) -> Path:
    artifact = _wheel(path, version=version, git_sha=git_sha)
    migration = """
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from .schema_compatibility import MAX_SUPPORTED_SCHEMA_VERSION

@dataclass(frozen=True)
class Result:
    previous_version: int
    current_version: int
    backup_path: None = None

def migrate_database(path: Path, *, create_backup: bool = True) -> Result:
    del create_backup
    connection = sqlite3.connect(path)
    try:
        previous = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if previous > MAX_SUPPORTED_SCHEMA_VERSION:
            raise RuntimeError("newer schema")
        connection.execute(f"PRAGMA user_version = {MAX_SUPPORTED_SCHEMA_VERSION}")
        connection.commit()
        return Result(previous, MAX_SUPPORTED_SCHEMA_VERSION)
    finally:
        connection.close()
    """
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("hermes_codex_router/__init__.py", "")
        archive.writestr("hermes_codex_router/migrations.py", migration)
    return artifact


class ReleaseDryRunTests(unittest.TestCase):
    def test_rollout_and_runtime_rollback_use_only_temporary_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = _runnable_wheel(
                root / "agents_projects_hub-0.7.0-py3-none-any.whl",
                version="0.7.0",
                git_sha="a" * 40,
            )
            rollback = _runnable_wheel(
                root / "agents_projects_hub-0.6.0-py3-none-any.whl",
                version="0.6.0",
                git_sha="b" * 40,
            )

            report = run_release_dry_run(active, rollback)

            self.assertEqual(report.schema_before, 20)
            self.assertEqual(report.schema_after_rollout, 21)
            self.assertEqual(report.schema_after_rollback, 21)
            self.assertTrue(report.durable_work_preserved)
            self.assertTrue(report.rollback_pointer_restored)
            self.assertTrue(report.manifest_verified)
            self.assertTrue(report.temporary_state_only)
            self.assertFalse(report.service_actions)
            self.assertFalse(report.network_actions)
            self.assertTrue(report_dict(report)["ok"])
            self.assertTrue(active.is_file())
            self.assertTrue(rollback.is_file())


if __name__ == "__main__":
    unittest.main()
