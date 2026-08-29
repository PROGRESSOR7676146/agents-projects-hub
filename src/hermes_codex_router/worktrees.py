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
