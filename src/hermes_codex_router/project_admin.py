from __future__ import annotations

import json
import os
import subprocess
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


def prepare_project_root(base_root: Path, project_id: str) -> Path:
    """Create one bounded project directory and initialize Git when safe."""
    if PROJECT_ID.fullmatch(project_id) is None:
        raise RegistryError(f"invalid project_id: {project_id}")
    base = base_root.expanduser().resolve(strict=True)
    if not base.is_dir():
        raise RegistryError("allowed project root is not a directory")
    target = (base / project_id).resolve(strict=False)
    try:
        relative = target.relative_to(base)
    except ValueError:
        raise RegistryError("project root is outside allowed_root") from None
    if len(relative.parts) != 1:
        raise RegistryError("project root must be a direct child of allowed_root")
    if target.exists():
        if not target.is_dir():
            raise RegistryError("project root exists and is not a directory")
        if not (target / ".git").exists() and any(target.iterdir()):
            raise RegistryError("existing non-empty directory is not a Git root")
    else:
        target.mkdir(mode=0o700)
    if not (target / ".git").exists():
        try:
            subprocess.run(
                ("git", "init", "--initial-branch=main", str(target)),
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RegistryError("could not initialize project Git repository") from exc
    canonical = target.resolve(strict=True)
    try:
        result = subprocess.run(
            ("git", "-C", str(canonical), "rev-parse", "--show-toplevel"),
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if Path(result.stdout.strip()).resolve(strict=True) != canonical:
            raise RegistryError("project directory is not the exact Git root")
    except RegistryError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RegistryError("project directory is not a valid Git root") from exc
    return canonical


def ensure_project(
    registry_path: Path,
    *,
    project_id: str,
    display_name: str,
    root: Path,
) -> None:
    """Idempotently persist the exact project prepared by one workflow."""
    registry = load_registry(registry_path)
    canonical = root.expanduser().resolve(strict=True)
    matches = [
        item
        for item in registry.projects
        if item.project_id == project_id or item.root == canonical
    ]
    if matches:
        item = matches[0]
        if (
            len(matches) == 1
            and item.project_id == project_id
            and item.root == canonical
            and item.enabled
        ):
            return
        raise RegistryError("project registry identity conflicts with onboarding workflow")
    add_project(
        registry_path,
        project_id=project_id,
        display_name=display_name,
        topic_name=display_name,
        root=canonical,
    )


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
