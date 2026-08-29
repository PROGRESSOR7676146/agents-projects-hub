from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
SERVICE_UNIT = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
SUPPORTED_RUNTIMES = {"codex", "hermes", "gemini", "antigravity", "opencode", "api"}


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
    executable: str | None = None
    runtime_home: Path | None = None
    service_unit: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalSettings:
    backend: str
    program: str | None
    wsl_distro: str


@dataclass(frozen=True, slots=True)
class RecoveryPlaneSettings:
    enabled: bool
    hermes_service: str
    tlive_service: str
    hermes_config_path: Path | None
    tlive_config_path: Path | None
    hermes_notify_target: str


@dataclass(frozen=True, slots=True)
class OperationalAlertSettings:
    telegram_chat_id: int | None
    telegram_thread_id: int | None


@dataclass(frozen=True, slots=True)
class HubConfig:
    schema_version: int
    owner_user_ids: tuple[int, ...]
    registry_path: Path
    state_path: Path
    codex_socket_path: Path
    manage_codex_server: bool
    terminal: TerminalSettings
    projects: tuple[ProjectBinding, ...]
    agents: tuple[AgentDefinition, ...]
    direct_message_project_id: str | None = None
    recovery_plane: RecoveryPlaneSettings = field(
        default_factory=lambda: RecoveryPlaneSettings(
            False,
            "hermes-gateway.service",
            "tlive.service",
            None,
            None,
            "telegram",
        )
    )
    operational_alerts: OperationalAlertSettings = field(
        default_factory=lambda: OperationalAlertSettings(None, None)
    )
    codex_multi_auth_dir: Path | None = None
    codex_multi_auth_executable: Path | None = None
    codex_stdio_executable: Path | None = None
    codex_account_hints: dict[int, str] = field(default_factory=dict)

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
    multi_auth_value = root.get("codex_multi_auth_dir")
    codex_multi_auth_dir = None
    if multi_auth_value is not None:
        codex_multi_auth_dir = _absolute_path(
            multi_auth_value, "codex_multi_auth_dir", must_exist=True
        )
        if not codex_multi_auth_dir.is_dir():
            raise HubConfigError("codex_multi_auth_dir must be a directory")
        if codex_multi_auth_dir.stat().st_mode & 0o077:
            raise HubConfigError("codex_multi_auth_dir must have mode 0700")
    multi_auth_executable_value = root.get("codex_multi_auth_executable")
    codex_multi_auth_executable = None
    if multi_auth_executable_value is not None:
        codex_multi_auth_executable = _absolute_path(
            multi_auth_executable_value, "codex_multi_auth_executable", must_exist=True
        )
        if not codex_multi_auth_executable.is_file() or not (
            codex_multi_auth_executable.stat().st_mode & 0o111
        ):
            raise HubConfigError("codex_multi_auth_executable must be executable")
    raw_account_hints = root.get("codex_account_hints", {})
    if not isinstance(raw_account_hints, dict):
        raise HubConfigError("codex_account_hints must be an object")
    codex_account_hints: dict[int, str] = {}
    for raw_index, raw_hint in raw_account_hints.items():
        try:
            account_index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise HubConfigError("codex_account_hints has an invalid index") from exc
        if (
            account_index <= 0
            or not isinstance(raw_hint, str)
            or not re.fullmatch(r"[A-Za-z0-9]{3}", raw_hint)
        ):
            raise HubConfigError("codex_account_hints values must be three characters")
        codex_account_hints[account_index] = raw_hint
    stdio_executable_value = root.get("codex_stdio_executable")
    codex_stdio_executable = None
    if stdio_executable_value is not None:
        codex_stdio_executable = _absolute_path(
            stdio_executable_value, "codex_stdio_executable", must_exist=True
        )
        if not codex_stdio_executable.is_file() or not (
            codex_stdio_executable.stat().st_mode & 0o111
        ):
            raise HubConfigError("codex_stdio_executable must be executable")
        if manage_codex_server:
            raise HubConfigError(
                "manage_codex_server and codex_stdio_executable are mutually exclusive"
            )

    terminal_data = _object(root.get("terminal", {}), "terminal")
    terminal_backend = terminal_data.get("backend", "auto")
    if terminal_backend not in {"auto", "wsl", "linux", "macos", "tmux-only"}:
        raise HubConfigError("terminal.backend is unsupported")
    terminal_program = terminal_data.get("program")
    if terminal_program is not None and (
        not isinstance(terminal_program, str) or not terminal_program.strip()
    ):
        raise HubConfigError("terminal.program must be a non-empty string")
    wsl_distro = terminal_data.get("wsl_distro", "Ubuntu")
    if not isinstance(wsl_distro, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", wsl_distro):
        raise HubConfigError("terminal.wsl_distro is invalid")

    recovery_data = _object(root.get("recovery_plane", {}), "recovery_plane")
    recovery_enabled = recovery_data.get("enabled", False)
    if not isinstance(recovery_enabled, bool):
        raise HubConfigError("recovery_plane.enabled must be boolean")
    hermes_service = recovery_data.get("hermes_service", "hermes-gateway.service")
    tlive_service = recovery_data.get("tlive_service", "tlive.service")
    hermes_notify_target = recovery_data.get("hermes_notify_target", "telegram")
    for label, value in (
        ("hermes_service", hermes_service),
        ("tlive_service", tlive_service),
    ):
        if not isinstance(value, str) or not SERVICE_UNIT.fullmatch(value):
            raise HubConfigError(f"recovery_plane.{label} is invalid")
    if not isinstance(hermes_notify_target, str) or not re.fullmatch(
        r"telegram(?::(-?\d+)(?::\d+)?)?", hermes_notify_target
    ):
        raise HubConfigError("recovery_plane.hermes_notify_target is invalid")
    hermes_config_path = None
    tlive_config_path = None
    if recovery_enabled:
        hermes_config_path = _absolute_path(
            recovery_data.get("hermes_config_path"),
            "recovery_plane.hermes_config_path",
            must_exist=True,
        )
        tlive_config_path = _absolute_path(
            recovery_data.get("tlive_config_path"),
            "recovery_plane.tlive_config_path",
            must_exist=True,
        )

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

    alerts_data = _object(root.get("operational_alerts", {}), "operational_alerts")
    alerts_project_id = alerts_data.get("project_id")
    alerts_thread_id = alerts_data.get("telegram_thread_id")
    alerts_chat_id: int | None = None
    if alerts_project_id is not None or alerts_thread_id is not None:
        if alerts_project_id != "hub":
            raise HubConfigError("operational_alerts.project_id must be hub")
        if not isinstance(alerts_project_id, str) or not IDENTIFIER.fullmatch(alerts_project_id):
            raise HubConfigError("operational_alerts.project_id is invalid")
        matching_projects = [item for item in projects if item.project_id == alerts_project_id]
        if len(matching_projects) != 1 or matching_projects[0].telegram_chat_id is None:
            raise HubConfigError("operational_alerts.project_id is not a bound project")
        if not isinstance(alerts_thread_id, int) or alerts_thread_id <= 1:
            raise HubConfigError("operational_alerts.telegram_thread_id is invalid")
        alerts_chat_id = matching_projects[0].telegram_chat_id

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
        if not username.casefold().endswith("bot"):
            raise HubConfigError(f"telegram_username for {agent_id} must end in bot")
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
        default_model = data.get(
            "default_model", "gpt-5.6-sol" if runtime == "codex" else "unknown"
        )
        default_effort = data.get("default_effort", "high")
        if not isinstance(default_model, str) or not default_model.strip():
            raise HubConfigError(f"default_model is invalid for {agent_id}")
        if default_effort not in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        }:
            raise HubConfigError(f"default_effort is invalid for {agent_id}")
        executable = data.get("executable")
        if executable is not None and (not isinstance(executable, str) or not executable.strip()):
            raise HubConfigError(f"executable is invalid for {agent_id}")
        runtime_home_value = data.get("runtime_home")
        runtime_home = None
        if runtime_home_value is not None:
            runtime_home = _absolute_path(
                runtime_home_value, f"runtime_home for {agent_id}", must_exist=True
            )
            if not runtime_home.is_dir():
                raise HubConfigError(f"runtime_home for {agent_id} is not a directory")
            if runtime_home.stat().st_mode & 0o077:
                raise HubConfigError(f"runtime_home for {agent_id} must have mode 0700")
        service_unit = data.get("service_unit")
        if service_unit is not None and (
            not isinstance(service_unit, str) or not SERVICE_UNIT.fullmatch(service_unit)
        ):
            raise HubConfigError(f"service_unit is invalid for {agent_id}")
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
                executable=executable.strip() if executable else None,
                runtime_home=runtime_home,
                service_unit=service_unit,
            )
        )

    direct_message_project_id = root.get("direct_message_project_id")
    if direct_message_project_id is not None and (
        not isinstance(direct_message_project_id, str)
        or direct_message_project_id not in project_ids
    ):
        raise HubConfigError(
            "direct_message_project_id must reference a registered project"
        )

    return HubConfig(
        schema_version=1,
        owner_user_ids=tuple(raw_owners),
        registry_path=registry_path,
        state_path=state_path,
        codex_socket_path=codex_socket_path,
        manage_codex_server=manage_codex_server,
        terminal=TerminalSettings(
            backend=terminal_backend,
            program=terminal_program.strip() if terminal_program else None,
            wsl_distro=wsl_distro,
        ),
        recovery_plane=RecoveryPlaneSettings(
            enabled=recovery_enabled,
            hermes_service=hermes_service,
            tlive_service=tlive_service,
            hermes_config_path=hermes_config_path,
            tlive_config_path=tlive_config_path,
            hermes_notify_target=hermes_notify_target,
        ),
        operational_alerts=OperationalAlertSettings(alerts_chat_id, alerts_thread_id),
        projects=tuple(projects),
        agents=tuple(agents),
        direct_message_project_id=direct_message_project_id,
        codex_multi_auth_dir=codex_multi_auth_dir,
        codex_multi_auth_executable=codex_multi_auth_executable,
        codex_stdio_executable=codex_stdio_executable,
        codex_account_hints=codex_account_hints,
    )
