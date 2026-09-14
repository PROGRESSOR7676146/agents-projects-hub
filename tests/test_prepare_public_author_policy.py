from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_public_author_policy.py"
EXAMPLE_EMAIL = "owner@public." + "example.invalid"


class PreparePublicAuthorPolicyTests(unittest.TestCase):
    def run_script(
        self,
        directory: Path,
        *,
        value: str | None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        github_env = directory / "github-env"
        github_env.touch()
        environment = {
            **os.environ,
            "RUNNER_TEMP": str(directory),
            "GITHUB_ENV": str(github_env),
        }
        if value is None:
            environment.pop("HUB_PUBLIC_GIT_AUTHOR_EMAIL", None)
        else:
            environment["HUB_PUBLIC_GIT_AUTHOR_EMAIL"] = value
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, github_env

    def test_writes_fictional_value_to_private_external_file_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            result, github_env = self.run_script(directory, value=EXAMPLE_EMAIL)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            name, separator, raw_path = github_env.read_text(encoding="utf-8").partition("=")
            self.assertEqual(name, "HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE")
            self.assertEqual(separator, "=")
            policy_path = Path(raw_path.rstrip("\n"))
            self.assertEqual(policy_path.read_bytes(), EXAMPLE_EMAIL.encode())
            details = policy_path.stat()
            self.assertEqual(stat.S_IMODE(details.st_mode), 0o600)
            self.assertEqual(details.st_uid, os.getuid())
            self.assertEqual(details.st_nlink, 1)
            policy_path.relative_to(directory)

    def test_missing_value_writes_nothing_and_cannot_skip_canonical_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            result, github_env = self.run_script(Path(directory_name), value=None)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertEqual(github_env.read_text(encoding="utf-8"), "")

    def test_value_and_private_path_never_appear_in_process_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            result, _ = self.run_script(Path(directory_name), value=EXAMPLE_EMAIL + "\nextra")
            combined = result.stdout + result.stderr
            self.assertEqual(combined, "")
            self.assertNotIn(EXAMPLE_EMAIL, combined)
            self.assertNotIn(directory_name, combined)
