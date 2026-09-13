"""One fail-closed resolver for static and dynamically onboarded project groups."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .hub_config import HubConfig
from .models import Project, ProjectRegistry
from .project_onboarding import ProjectGroupBinding, ProjectOnboardingStore
from .registry import RegistryError, load_registry
from .state import HubState


class ProjectResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedProject:
    project: Project
    registry: ProjectRegistry
    chat_id: int
    source: str
    receipt: ProjectGroupBinding | None


@dataclass(frozen=True, slots=True)
class ProjectResolutionIssue:
    chat_id: int
    error_code: str


def _validate_git_root(registry: ProjectRegistry, root: Path) -> None:
    try:
        canonical = root.resolve(strict=True)
        if not any(
            canonical.is_relative_to(allowed.resolve(strict=True))
            for allowed in registry.allowed_roots
        ):
            raise ProjectResolutionError("project_root_invalid")
        result = subprocess.run(
            ("git", "-C", str(canonical), "rev-parse", "--show-toplevel"),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        if Path(result.stdout.strip()).resolve(strict=True) != canonical:
            raise ProjectResolutionError("project_root_invalid")
    except ProjectResolutionError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ProjectResolutionError("project_root_invalid") from None


def resolve_project_context(
    config: HubConfig,
    state: HubState | None,
    *,
    chat_id: int,
    expected_project_id: str | None = None,
    expected_root: Path | None = None,
) -> ResolvedProject:
    """Resolve an exact group receipt against the current local registry and Git root."""
    try:
        registry = load_registry(config.registry_path)
        static = tuple(item for item in config.projects if item.telegram_chat_id == chat_id)
        receipt = None if state is None else ProjectOnboardingStore(state).binding_for_chat(chat_id)
        if len(static) > 1:
            raise ProjectResolutionError("project_binding_conflict")
        if static and receipt is not None and static[0].project_id != receipt.project_id:
            raise ProjectResolutionError("project_binding_conflict")
        if static:
            project_id = static[0].project_id
            source = "static+onboarding" if receipt is not None else "static"
        elif receipt is not None:
            project_id = receipt.project_id
            source = "onboarding"
        elif (
            chat_id > 0
            and chat_id in config.owner_user_ids
            and expected_project_id is not None
            and config.direct_message_project_id == expected_project_id
        ):
            project_id = expected_project_id
            source = "direct"
        else:
            raise ProjectResolutionError("project_binding_missing")
        if expected_project_id is not None and project_id != expected_project_id:
            raise ProjectResolutionError("project_binding_mismatch")
        project = registry.require_project(project_id)
        if receipt is not None and project.root != receipt.canonical_root:
            raise ProjectResolutionError("project_binding_mismatch")
        if expected_root is not None and project.root != expected_root.resolve(strict=True):
            raise ProjectResolutionError("project_binding_mismatch")
        _validate_git_root(registry, project.root)
        return ResolvedProject(project, registry, chat_id, source, receipt)
    except ProjectResolutionError:
        raise
    except (KeyError, RegistryError, OSError, ValueError):
        raise ProjectResolutionError("project_binding_invalid") from None


def list_resolved_project_groups(
    config: HubConfig,
    state: HubState | None,
    *,
    issues: list[ProjectResolutionIssue] | None = None,
) -> tuple[ResolvedProject, ...]:
    """List valid groups while reporting bounded per-binding failures."""
    chat_ids = [
        item.telegram_chat_id for item in config.projects if item.telegram_chat_id is not None
    ]
    if state is not None:
        chat_ids.extend(item.telegram_chat_id for item in ProjectOnboardingStore(state).bindings())
    resolved: list[ResolvedProject] = []
    for chat_id in dict.fromkeys(chat_ids):
        try:
            resolved.append(resolve_project_context(config, state, chat_id=chat_id))
        except ProjectResolutionError as exc:
            if issues is not None:
                issues.append(ProjectResolutionIssue(chat_id, str(exc)))
    duplicate_ids = {
        item.project.project_id
        for item in resolved
        if sum(other.project.project_id == item.project.project_id for other in resolved) > 1
    }
    if duplicate_ids:
        kept: list[ResolvedProject] = []
        for item in resolved:
            if item.project.project_id in duplicate_ids:
                if issues is not None:
                    issues.append(ProjectResolutionIssue(item.chat_id, "project_binding_conflict"))
            else:
                kept.append(item)
        resolved = kept
    return tuple(resolved)


def resolve_project_group(
    config: HubConfig,
    state: HubState | None,
    *,
    project_id: str,
    expected_root: Path | None = None,
) -> ResolvedProject:
    """Resolve the unique group for a project through the strict chat resolver."""
    candidate_chat_ids = [
        item.telegram_chat_id
        for item in config.projects
        if item.project_id == project_id and item.telegram_chat_id is not None
    ]
    if state is not None:
        candidate_chat_ids.extend(
            item.telegram_chat_id
            for item in ProjectOnboardingStore(state).bindings()
            if item.project_id == project_id
        )
    unique_chat_ids = tuple(dict.fromkeys(candidate_chat_ids))
    if not unique_chat_ids:
        raise ProjectResolutionError("project_binding_missing")
    if len(unique_chat_ids) > 1:
        raise ProjectResolutionError("project_binding_conflict")
    resolved = resolve_project_context(
        config,
        state,
        chat_id=next(iter(unique_chat_ids)),
        expected_project_id=project_id,
    )
    if expected_root is not None and resolved.project.root != expected_root.resolve(strict=True):
        raise ProjectResolutionError("project_binding_mismatch")
    return resolved
