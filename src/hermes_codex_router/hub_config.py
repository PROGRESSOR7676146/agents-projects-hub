from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
SUPPORTED_RUNTIMES = {"codex", "hermes", "gemini", "opencode", "api"}


class HubConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectBinding:
    project_id: str
    telegram_chat_id: int | None


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    display_name: str
    telegram_username: str
    runtime: str
    token_file: Path | None
    terminal_enabled: bool
    managed_externally: bool
    default_model: str
    default_effort: str


@dataclass(frozen=True, slots=True)
class HubConfig:
    schema_version: int
    owner_user_ids: tuple[int, ...]
    registry_path: Path
    state_path: Path
    codex_socket_path: Path
    manage_codex_server: bool
    projects: tuple[ProjectBinding, ...]
    agents: tuple[AgentDefinition, ...]

    def require_agent(self, agent_id: str) -> AgentDefinition:
        for agent in self.agents:
            if agent.agent_id == agent_id:
                return agent
        raise KeyError(f"unknown agent_id: {agent_id}")

    def project_for_chat(self, chat_id: int) -> ProjectBinding:
        for project in self.projects:
            if project.telegram_chat_id == chat_id:
                return project
        raise KeyError(f"unregistered Telegram project group: {chat_id}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HubConfigError(f"{label} must be an object")
    return value


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HubConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _absolute_path(value: Any, label: str, *, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise HubConfigError(f"{label} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HubConfigError(f"{label} must be absolute")
    try:
        return path.resolve(strict=must_exist)
    except OSError as exc:
        raise HubConfigError(f"cannot resolve {label}: {exc}") from exc


def _private_token_file(value: Any, agent_id: str) -> Path:
    path = _absolute_path(value, f"token_file for {agent_id}", must_exist=True)
    if not path.is_file():
        raise HubConfigError(f"token_file for {agent_id} is not a file")
    if path.stat().st_mode & 0o077:
        raise HubConfigError(f"token_file for {agent_id} must have mode 0600")
    token = path.read_text(encoding="utf-8").strip()
    if not token or "\n" in token or ":" not in token:
        raise HubConfigError(f"token_file for {agent_id} is malformed")
    return path


def load_hub_config(path: Path, *, allow_unbound: bool = False) -> HubConfig:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HubConfigError(f"cannot read hub config: {exc}") from exc
    root = _object(document, "hub config")
    if root.get("schema_version") != 1:
        raise HubConfigError("schema_version must be 1")

    raw_owners = root.get("owner_user_ids")
    if not isinstance(raw_owners, list) or not raw_owners:
        raise HubConfigError("owner_user_ids must be a non-empty array")
    if not all(isinstance(value, int) and value > 0 for value in raw_owners):
        raise HubConfigError("owner_user_ids must contain positive integers")
    if len(set(raw_owners)) != len(raw_owners):
        raise HubConfigError("owner_user_ids contains duplicates")

    registry_path = _absolute_path(root.get("registry_path"), "registry_path", must_exist=True)
    state_path = _absolute_path(root.get("state_path"), "state_path", must_exist=False)
    codex_socket_path = _absolute_path(
        root.get(
            "codex_socket_path",
            str(Path.home() / ".codex/app-server-control/app-server-control.sock"),
        ),
        "codex_socket_path",
        must_exist=False,
    )
    manage_codex_server = root.get("manage_codex_server", False)
    if not isinstance(manage_codex_server, bool):
        raise HubConfigError("manage_codex_server must be boolean")

    raw_projects = root.get("projects")
    if not isinstance(raw_projects, list) or not raw_projects:
        raise HubConfigError("projects must be a non-empty array")
    projects: list[ProjectBinding] = []
    project_ids: set[str] = set()
    chat_ids: set[int] = set()
    for index, item in enumerate(raw_projects):
        data = _object(item, f"projects[{index}]")
        project_id = _text(data, "project_id")
        if not IDENTIFIER.fullmatch(project_id) or project_id in project_ids:
            raise HubConfigError(f"invalid or duplicate project_id: {project_id}")
        chat_id = data.get("telegram_chat_id")
        if chat_id is None:
            if not allow_unbound:
                raise HubConfigError(f"project {project_id} has an unbound Telegram group")
        elif (
            not isinstance(chat_id, int)
            or not str(chat_id).startswith("-100")
            or chat_id in chat_ids
        ):
            raise HubConfigError(f"invalid or duplicate telegram_chat_id for {project_id}")
        project_ids.add(project_id)
        if chat_id is not None:
            chat_ids.add(chat_id)
        projects.append(ProjectBinding(project_id, chat_id))

    raw_agents = root.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise HubConfigError("agents must be a non-empty array")
    agents: list[AgentDefinition] = []
    agent_ids: set[str] = set()
    usernames: set[str] = set()
    for index, item in enumerate(raw_agents):
        data = _object(item, f"agents[{index}]")
        if "token" in data:
            raise HubConfigError("inline token fields are forbidden; use token_file")
        agent_id = _text(data, "agent_id")
        display_name = _text(data, "display_name")
        username = _text(data, "telegram_username").removeprefix("@")
        runtime = _text(data, "runtime")
        if not IDENTIFIER.fullmatch(agent_id) or agent_id in agent_ids:
            raise HubConfigError(f"invalid or duplicate agent_id: {agent_id}")
        if not USERNAME.fullmatch(username) or username.casefold() in usernames:
            raise HubConfigError(f"invalid or duplicate telegram_username: {username}")
        if runtime not in SUPPORTED_RUNTIMES:
            raise HubConfigError(f"unsupported runtime for {agent_id}: {runtime}")
        managed_externally = data.get("managed_externally", False)
        terminal_enabled = data.get("terminal_enabled", False)
        if not isinstance(managed_externally, bool) or not isinstance(terminal_enabled, bool):
            raise HubConfigError(f"boolean agent flags are invalid for {agent_id}")
        token_file = None
        if not managed_externally:
            token_file = _private_token_file(data.get("token_file"), agent_id)
        elif data.get("token_file") is not None:
            token_file = _private_token_file(data.get("token_file"), agent_id)
        agent_ids.add(agent_id)
        usernames.add(username.casefold())
        default_model = data.get("default_model", "gpt-5.6-sol" if runtime == "codex" else "unknown")
        default_effort = data.get("default_effort", "high")
        if not isinstance(default_model, str) or not default_model.strip():
            raise HubConfigError(f"default_model is invalid for {agent_id}")
        if default_effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
        }:
            raise HubConfigError(f"default_effort is invalid for {agent_id}")
        agents.append(
            AgentDefinition(
                agent_id=agent_id,
                display_name=display_name,
                telegram_username=username,
                runtime=runtime,
                token_file=token_file,
                terminal_enabled=terminal_enabled,
                managed_externally=managed_externally,
                default_model=default_model.strip(),
                default_effort=default_effort,
            )
        )

    return HubConfig(
        schema_version=1,
        owner_user_ids=tuple(raw_owners),
        registry_path=registry_path,
        state_path=state_path,
        codex_socket_path=codex_socket_path,
        manage_codex_server=manage_codex_server,
        projects=tuple(projects),
        agents=tuple(agents),
    )
