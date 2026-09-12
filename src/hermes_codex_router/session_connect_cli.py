"""Local assistant for issuing owner-scoped Telegram session-connect codes."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .codex_appserver import ConnectableCodexThread, RpcError
from .codex_session_adoption import (
    AdoptionError,
    list_connectable_codex_sessions,
    open_adoption_state,
)
from .hub_config import HubConfig
from .registry import load_registry
from .session_adoption_policy import supports_adoption
from .session_connect import ConnectCandidate, SessionConnectStore


class ConnectCliError(ValueError):
    def __init__(self, reason: str, *, temporary: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = 3 if temporary else 2


SessionLister = Callable[[HubConfig, Path], tuple[ConnectableCodexThread, ...]]
Input = Callable[[str], str]


def _numbered_choice(prompt: str, labels: tuple[str, ...], input_fn: Input) -> int:
    if not labels:
        raise ConnectCliError("selection_unavailable")
    for index, label in enumerate(labels, start=1):
        print(f"{index}. {label}")
    try:
        selected = int(input_fn(prompt).strip())
    except (ValueError, EOFError):
        raise ConnectCliError("selection_invalid") from None
    if not 1 <= selected <= len(labels):
        raise ConnectCliError("selection_invalid")
    return selected - 1


def prepare_connect_code(
    config: HubConfig,
    *,
    owner_user_id: int | None = None,
    project_id: str | None = None,
    codex_thread_id: str | None = None,
    interactive: bool = True,
    input_fn: Input = input,
    lister: SessionLister = list_connectable_codex_sessions,
) -> dict[str, object]:
    if config.hub_bot is None or not supports_adoption(config):
        raise ConnectCliError("execution_mode_unsupported")
    owners = config.owner_user_ids
    if owner_user_id is None:
        if len(owners) == 1:
            owner_user_id = owners[0]
        elif interactive:
            selected = _numbered_choice(
                "Owner: ", tuple(f"Owner {value}" for value in owners), input_fn
            )
            owner_user_id = owners[selected]
        else:
            raise ConnectCliError("owner_selection_required")
    if owner_user_id not in owners:
        raise ConnectCliError("owner_not_authorized")

    registry = load_registry(config.registry_path)
    registered = {
        binding.project_id for binding in config.projects if binding.telegram_chat_id is not None
    }
    projects = tuple(
        project
        for project in registry.projects
        if project.enabled and project.project_id in registered
    )
    if project_id is None:
        if interactive:
            selected = _numbered_choice(
                "Project: ", tuple(project.display_name for project in projects), input_fn
            )
            project = projects[selected]
        else:
            raise ConnectCliError("project_selection_required")
    else:
        try:
            project = next(item for item in projects if item.project_id == project_id)
        except StopIteration:
            raise ConnectCliError("project_unavailable") from None

    try:
        sessions = lister(config, project.root)
    except (OSError, EOFError, TimeoutError, RpcError):
        raise ConnectCliError("metadata_unavailable", temporary=True) from None
    if not sessions:
        raise ConnectCliError("no_sessions")
    if codex_thread_id is None:
        if not interactive:
            raise ConnectCliError("session_selection_required")
        selected = _numbered_choice(
            "Saved session: ", tuple(item.safe_label for item in sessions), input_fn
        )
        source = sessions[selected]
    else:
        try:
            source = next(item for item in sessions if item.thread_id == codex_thread_id)
        except StopIteration:
            raise ConnectCliError("session_unavailable") from None

    agent = config.require_agent("codex")
    try:
        with open_adoption_state(config.state_path, writable=True) as state:
            issued = SessionConnectStore(state).issue_code(
                owner_user_id=owner_user_id,
                project_id=project.project_id,
                canonical_root=project.root,
                source=ConnectCandidate(
                    "",
                    source.thread_id,
                    source.safe_label,
                    source.updated_at,
                ),
                model=agent.default_model,
                effort=agent.default_effort,
            )
    except AdoptionError as exc:
        raise ConnectCliError(exc.reason, temporary=exc.exit_code == 3) from None
    return {
        "format_version": 1,
        "ok": True,
        "project_id": project.project_id,
        "session_label": source.safe_label,
        "expires_at": issued.expires_at,
        "telegram_command": f"/connect {issued.code}",
        "next_action": "send_command_to_hub_or_project_topic",
    }
