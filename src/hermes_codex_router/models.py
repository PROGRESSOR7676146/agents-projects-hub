from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    display_name: str
    topic_name: str
    root: Path
    sandbox: str = "workspace-write"
    approval_policy: str = "on-request"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ProjectRegistry:
    schema_version: int
    allowed_roots: tuple[Path, ...]
    projects: tuple[Project, ...]

    def require_project(self, project_id: str) -> Project:
        for project in self.projects:
            if project.project_id == project_id and project.enabled:
                return project
        raise KeyError(f"unknown or disabled project_id: {project_id}")
