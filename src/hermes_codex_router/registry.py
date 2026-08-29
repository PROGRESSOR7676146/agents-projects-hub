from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Project, ProjectRegistry

PROJECT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
ALLOWED_SANDBOXES = {"read-only", "workspace-write"}
ALLOWED_APPROVAL_POLICIES = {"on-request"}


class RegistryError(ValueError):
    pass


def _expect_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be an object")
    return value


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{key} must be a non-empty string")
    return value.strip()


def _resolve_absolute(raw: str, label: str, *, require_exists: bool) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RegistryError(f"{label} must be absolute")
    resolved = path.resolve(strict=require_exists)
    if require_exists and not resolved.is_dir():
        raise RegistryError(f"{label} is not a directory: {resolved}")
    return resolved


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def load_registry(path: Path, *, require_exists: bool = True) -> ProjectRegistry:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read registry: {exc}") from exc
    root = _expect_dict(document, "registry")
    if root.get("schema_version") != 1:
        raise RegistryError("schema_version must be 1")

    raw_allowed = root.get("allowed_roots")
    if not isinstance(raw_allowed, list) or not raw_allowed:
        raise RegistryError("allowed_roots must be a non-empty array")
    allowed_roots = tuple(
        _resolve_absolute(str(item), "allowed_root", require_exists=require_exists)
        for item in raw_allowed
    )

    raw_projects = root.get("projects")
    if not isinstance(raw_projects, list):
        raise RegistryError("projects must be an array")

    projects: list[Project] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_roots: set[Path] = set()
    for index, item in enumerate(raw_projects):
        data = _expect_dict(item, f"projects[{index}]")
        project_id = _required_text(data, "project_id")
        if not PROJECT_ID.fullmatch(project_id):
            raise RegistryError(f"invalid project_id: {project_id}")
        display_name = _required_text(data, "display_name")
        topic_name = _required_text(data, "topic_name")
        if len(topic_name) > 128:
            raise RegistryError(f"topic_name is too long for {project_id}")
        project_root = _resolve_absolute(
            _required_text(data, "root"),
            f"root for {project_id}",
            require_exists=require_exists,
        )
        if not any(_is_within(project_root, allowed) for allowed in allowed_roots):
            raise RegistryError(f"root for {project_id} is outside allowed_roots")
        if require_exists and not (project_root / ".git").exists():
            raise RegistryError(f"root for {project_id} is not a Git root")

        sandbox = data.get("sandbox", "workspace-write")
        approval = data.get("approval_policy", "on-request")
        if sandbox not in ALLOWED_SANDBOXES:
            raise RegistryError(f"unsafe or unsupported sandbox for {project_id}: {sandbox}")
        if approval not in ALLOWED_APPROVAL_POLICIES:
            raise RegistryError(f"unsafe or unsupported approval policy for {project_id}: {approval}")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RegistryError(f"enabled must be boolean for {project_id}")

        folded_name = topic_name.casefold()
        if project_id in seen_ids:
            raise RegistryError(f"duplicate project_id: {project_id}")
        if folded_name in seen_names:
            raise RegistryError(f"duplicate topic_name: {topic_name}")
        if project_root in seen_roots:
            raise RegistryError(f"duplicate project root: {project_root}")
        seen_ids.add(project_id)
        seen_names.add(folded_name)
        seen_roots.add(project_root)
        projects.append(
            Project(
                project_id=project_id,
                display_name=display_name,
                topic_name=topic_name,
                root=project_root,
                sandbox=sandbox,
                approval_policy=approval,
                enabled=enabled,
            )
        )

    return ProjectRegistry(1, allowed_roots, tuple(projects))


def build_codex_argv(project: Project, *, session_id: str | None = None) -> tuple[str, ...]:
    """Build a shell-free interactive Codex command for local attach/debug use."""
    policy = (
        "-C",
        str(project.root),
        "--sandbox",
        project.sandbox,
        "--ask-for-approval",
        project.approval_policy,
    )
    if session_id is None:
        return ("codex", *policy)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", session_id):
        raise RegistryError("invalid Codex session id")
    return ("codex", "resume", session_id, *policy)
