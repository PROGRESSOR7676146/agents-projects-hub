from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.models import Project
from hermes_codex_router.worktrees import WorktreeError, create_worktree


class WorktreeTests(unittest.TestCase):
    def test_create_uses_git_argv_and_separate_sibling_path(self) -> None:
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Project"
            (root / ".git").mkdir(parents=True)
            project = Project("project", "Project", "Project", root)

            def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(argv)
                Path(argv[-1]).mkdir()
                return subprocess.CompletedProcess(argv, 0, "", "")

            path, branch = create_worktree(project, "backend", run=fake_run)
            self.assertEqual(path, Path(directory) / "Project-backend")
            self.assertEqual(branch, "lane/backend")
        self.assertEqual(calls[0][:4], ("git", "-C", str(root), "worktree"))

    def test_rejects_branch_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Project"
            (root / ".git").mkdir(parents=True)
            project = Project("project", "Project", "Project", root)
            with self.assertRaises(WorktreeError):
                create_worktree(project, "lane", branch_name="bad..branch")


if __name__ == "__main__":
    unittest.main()
