from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .registry import PROJECT_ID, RegistryError, load_registry


@contextmanager
def registry_lock(path: Path) -> Iterator[None]:
    """Serialize local registry read/modify/write operations across processes."""
    descriptor = os.open(path.with_name(f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path.with_name(f".{path.name}.lock"), 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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
    with registry_lock(registry_path):
        _add_project_unlocked(
            registry_path,
            project_id=project_id,
            display_name=display_name,
            topic_name=topic_name,
            root=root,
        )


def _add_project_unlocked(
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
    try:
        _atomic_write(registry_path, document)
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
    with registry_lock(registry_path):
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
        _add_project_unlocked(
            registry_path,
            project_id=project_id,
            display_name=display_name,
            topic_name=display_name,
            root=canonical,
        )


def set_project_enabled(registry_path: Path, project_id: str, enabled: bool) -> None:
    with registry_lock(registry_path):
        document = _read(registry_path)
        projects = document.get("projects")
        if not isinstance(projects, list):
            raise RegistryError("projects must be an array")
        selected: dict[str, object] | None = None
        previous: object = None
        for value in projects:
            if isinstance(value, dict) and value.get("project_id") == project_id:
                previous = value.get("enabled", True)
                value["enabled"] = enabled
                selected = value
                break
        if selected is None:
            raise RegistryError(f"unknown project_id: {project_id}")
        try:
            _atomic_write(registry_path, document)
            load_registry(registry_path)
        except Exception:
            selected["enabled"] = previous
            _atomic_write(registry_path, document)
            raise
