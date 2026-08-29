from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from .models import Project

LANE_ID = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
Run = Callable[..., subprocess.CompletedProcess[str]]


class WorktreeError(RuntimeError):
    pass


def lane_path(project: Project, lane_id: str) -> Path:
    if not LANE_ID.fullmatch(lane_id):
        raise WorktreeError("invalid lane id")
    return project.root.parent / f"{project.root.name}-{lane_id}"


def create_worktree(
    project: Project,
    lane_id: str,
    *,
    branch_name: str | None = None,
    run: Run = subprocess.run,
) -> tuple[Path, str]:
    path = lane_path(project, lane_id)
    branch = branch_name or f"lane/{lane_id}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch) or ".." in branch:
        raise WorktreeError("invalid branch name")
    if path.exists():
        raise WorktreeError(f"worktree path already exists: {path}")
    run(
        ("git", "-C", str(project.root), "worktree", "add", "-b", branch, str(path)),
        check=True,
        capture_output=True,
        text=True,
    )
    return path.resolve(strict=True), branch


def cleanup_worktree(
    project: Project,
    lane_id: str,
    *,
    recorded_path: Path,
    run: Run = subprocess.run,
) -> None:
    expected_path = lane_path(project, lane_id).absolute()
    recorded_absolute = recorded_path.expanduser().absolute()
    if recorded_absolute != expected_path:
        raise WorktreeError("recorded worktree path does not match the derived lane path")
    if recorded_absolute.is_symlink():
        raise WorktreeError("refusing to clean up a symlinked worktree path")
    expected = expected_path.resolve(strict=True)
    actual = recorded_path.expanduser().resolve(strict=True)
    if actual != expected:
        raise WorktreeError("recorded worktree path does not match the derived lane path")
    root = project.root.expanduser().resolve(strict=True)
    listed = run(
        ("git", "-C", str(root), "worktree", "list", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    )
    registered = {
        Path(line.removeprefix("worktree ")).absolute()
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    }
    if expected_path not in registered:
        raise WorktreeError("derived lane path is not a registered Git worktree")
    run(
        ("git", "-C", str(root), "worktree", "remove", str(actual)),
        check=True,
        capture_output=True,
        text=True,
    )
    run(
        ("git", "-C", str(root), "worktree", "prune"),
        check=True,
        capture_output=True,
        text=True,
    )
