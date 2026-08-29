from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.models import Project
from hermes_codex_router.worktrees import WorktreeError, cleanup_worktree, create_worktree


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

    def test_cleanup_uses_recorded_exact_path_without_force(self) -> None:
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Project"
            (root / ".git").mkdir(parents=True)
            lane = Path(directory) / "Project-backend"
            lane.mkdir()
            project = Project("project", "Project", "Project", root)

            def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(argv)
                if argv[-2:] == ("list", "--porcelain"):
                    return subprocess.CompletedProcess(argv, 0, f"worktree {lane}\n", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            cleanup_worktree(project, "backend", recorded_path=lane, run=fake_run)

        self.assertEqual(
            calls,
            [
                ("git", "-C", str(root), "worktree", "list", "--porcelain"),
                ("git", "-C", str(root), "worktree", "remove", str(lane)),
                ("git", "-C", str(root), "worktree", "prune"),
            ],
        )

    def test_cleanup_rejects_path_not_derived_from_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Project"
            (root / ".git").mkdir(parents=True)
            unrelated = Path(directory) / "unrelated"
            unrelated.mkdir()
            project = Project("project", "Project", "Project", root)
            with self.assertRaisesRegex(WorktreeError, "recorded worktree path"):
                cleanup_worktree(project, "backend", recorded_path=unrelated)

    def test_cleanup_rejects_symlink_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Project"
            (root / ".git").mkdir(parents=True)
            target = Path(directory) / "Project-other"
            target.mkdir()
            lane = Path(directory) / "Project-backend"
            lane.symlink_to(target, target_is_directory=True)
            project = Project("project", "Project", "Project", root)
            with self.assertRaisesRegex(WorktreeError, "symlinked"):
                cleanup_worktree(project, "backend", recorded_path=lane)


if __name__ == "__main__":
    unittest.main()
