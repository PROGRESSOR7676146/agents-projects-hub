from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .registry import PROJECT_ID, RegistryError, load_registry


def _read(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read registry: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistryError("registry must be an object")
    return value


def _atomic_write(path: Path, document: dict[str, object]) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def add_project(
    registry_path: Path,
    *,
    project_id: str,
    display_name: str,
    topic_name: str,
    root: Path,
) -> None:
    if not PROJECT_ID.fullmatch(project_id):
        raise RegistryError(f"invalid project_id: {project_id}")
    canonical_root = root.expanduser().resolve(strict=True)
    if not (canonical_root / ".git").exists():
        raise RegistryError(f"root is not a Git worktree: {canonical_root}")
    document = _read(registry_path)
    allowed = document.get("allowed_roots")
    projects = document.get("projects")
    if not isinstance(allowed, list) or not isinstance(projects, list):
        raise RegistryError("registry arrays are invalid")
    allowed_paths = [Path(str(value)).expanduser().resolve(strict=True) for value in allowed]
    if not any(
        canonical_root == parent or canonical_root.is_relative_to(parent)
        for parent in allowed_paths
    ):
        raise RegistryError("project root is outside existing allowed_roots")
    projects.append(
        {
            "project_id": project_id,
            "display_name": display_name,
            "topic_name": topic_name,
            "root": str(canonical_root),
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "enabled": True,
        }
    )
    _atomic_write(registry_path, document)
    try:
        load_registry(registry_path)
    except Exception:
        projects.pop()
        _atomic_write(registry_path, document)
        raise


def set_project_enabled(registry_path: Path, project_id: str, enabled: bool) -> None:
    document = _read(registry_path)
    projects = document.get("projects")
    if not isinstance(projects, list):
        raise RegistryError("projects must be an array")
    found = False
    for value in projects:
        if isinstance(value, dict) and value.get("project_id") == project_id:
            value["enabled"] = enabled
            found = True
            break
    if not found:
        raise RegistryError(f"unknown project_id: {project_id}")
    _atomic_write(registry_path, document)
    load_registry(registry_path)
