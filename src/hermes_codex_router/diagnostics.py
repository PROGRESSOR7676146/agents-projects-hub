from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .codex_proxy_health import (
    probe_codex_config_proxy,
    probe_codex_multi_auth_accounts,
    probe_codex_runtime_proxy,
)
from .hermes_health import probe_gateway_heartbeat, probe_hermes_group_policy
from .hub_config import HubConfig
from .migrations import LATEST_SCHEMA_VERSION
from .provider_telemetry import probe_antigravity_telemetry
from .recovery_plane import (
    RecoveryPlaneProbe,
    probe_recovery_plane,
    probe_supervisor_service,
    probe_tlive_runtime,
)
from .registry import load_registry
from .state import HubState, TelegramContractProvenance
from .terminal_runtime import TerminalRuntime


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _command(name: str, *, required: bool = True) -> Check:
    found = shutil.which(name)
    return Check(f"command:{name}", found is not None, found or "not found", required)


def _socket_check(path: Path, *, required: bool = True) -> Check:
    if not path.exists():
        return Check("codex_socket", False, f"missing: {path}", required)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        return Check("codex_socket", False, str(exc), required)
    return Check("codex_socket", stat.S_ISSOCK(mode), str(path), required)


def _service_check(
    unit: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> Check:
    state = probe_supervisor_service(
        ("systemctl", "--user", "is-active", "--quiet", unit),
        run=run,
    )
    return Check(f"service:{unit}", state == "active", state)


def _telegram_contract_checks(
    provenance: tuple[TelegramContractProvenance, ...],
) -> list[Check]:
    checks: list[Check] = []
    for item in provenance:
        version = int(item["acknowledged_version"])
        checks.append(
            Check(
                f"telegram_contract:{item['session_id']}",
                True,
                " ".join(
                    (
                        f"agent={item['agent_id']}",
                        f"status={item['status']}",
                        "provider_bound=" + ("yes" if item["provider_bound"] else "no"),
                        f"acknowledged=v{version}",
                    )
                ),
                required=False,
            )
        )
    return checks


def run_doctor(config: HubConfig) -> dict[str, object]:
    checks: list[Check] = []
    try:
        registry = load_registry(config.registry_path)
        checks.append(Check("registry", True, f"{len(registry.projects)} projects"))
    except Exception as exc:
        checks.append(Check("registry", False, str(exc)))

    try:
        state = HubState.open(config.state_path)
        try:
            version = state.schema_version
            checks.append(
                Check(
                    "state_schema",
                    version == LATEST_SCHEMA_VERSION,
                    f"version {version}/{LATEST_SCHEMA_VERSION}",
                )
            )
            checks.append(
                Check(
                    "state_permissions",
                    config.state_path.stat().st_mode & 0o077 == 0,
                    oct(config.state_path.stat().st_mode & 0o777),
                )
            )
            checks.extend(_telegram_contract_checks(state.telegram_contract_provenance()))
        finally:
            state.close()
    except Exception as exc:
        checks.append(Check("state", False, str(exc)))

    checks.extend([_command("git"), _command("codex"), _command("tmux")])
    for agent in config.agents:
        if agent.service_unit is not None:
            checks.append(_service_check(agent.service_unit))
        if agent.runtime in {"gemini", "antigravity", "opencode"}:
            executable = agent.executable or (
                "agy" if agent.runtime == "antigravity" else agent.runtime
            )
            if Path(executable).is_absolute():
                path = Path(executable)
                checks.append(
                    Check(
                        f"command:{agent.agent_id}",
                        path.is_file() and bool(path.stat().st_mode & 0o111),
                        str(path),
                    )
                )
            else:
                checks.append(_command(executable))
    for agent_id, settings in config.provider_telemetry.items():
        telemetry = probe_antigravity_telemetry(settings)
        checks.append(
            Check(
                f"provider_telemetry:{agent_id}",
                telemetry.ok,
                telemetry.detail,
                required=False,
            )
        )
    terminal = TerminalRuntime(
        socket_path=config.codex_socket_path,
        backend=config.terminal.backend,
        program=config.terminal.program,
        distro=config.terminal.wsl_distro,
    )
    checks.append(
        Check(
            "terminal_launcher",
            terminal.launcher_available(),
            f"backend={terminal.backend}, program={terminal.launcher_program() or 'manual tmux'}",
            required=False,
        )
    )
    socket = _socket_check(
        config.codex_socket_path,
        required=config.codex_stdio_executable is None,
    )
    checks.append(socket)
    if config.codex_multi_auth_dir is not None:
        proxy = probe_codex_runtime_proxy()
        checks.append(
            Check(
                "codex_multi_auth_runtime_proxy",
                proxy.ok,
                proxy.detail,
                required=False,
            )
        )
        ma_accounts = probe_codex_multi_auth_accounts(
            config.codex_multi_auth_dir,
            executable=(
                str(config.codex_multi_auth_executable)
                if config.codex_multi_auth_executable
                else "codex-multi-auth"
            ),
        )
        checks.append(
            Check(
                "codex_multi_auth_accounts",
                ma_accounts.ok,
                ma_accounts.detail,
                required=False,
            )
        )
    config_proxy = probe_codex_config_proxy()
    checks.append(
        Check(
            "codex_config_proxy",
            config_proxy.ok,
            config_proxy.detail,
            required=False,
        )
    )
    if config.codex_stdio_executable is not None:
        checks.append(
            Check(
                "codex_stdio_fallback",
                True,
                str(config.codex_stdio_executable),
            )
        )
    checks.append(
        Check(
            "hermes_state_environment",
            os.getenv("HERMES_PROJECT_HUB_STATE") in {None, str(config.state_path)},
            os.getenv("HERMES_PROJECT_HUB_STATE", "not set in this process"),
            required=False,
        )
    )

    recovery_available: bool | None = None
    if config.recovery_plane.enabled:
        assert config.recovery_plane.hermes_config_path is not None
        assert config.recovery_plane.tlive_config_path is not None
        heartbeat = probe_gateway_heartbeat(
            config.recovery_plane.hermes_config_path.parent / "state" / "gateway.heartbeat"
        )
        recovery = probe_recovery_plane(
            RecoveryPlaneProbe(
                hermes_service=config.recovery_plane.hermes_service,
                tlive_service=config.recovery_plane.tlive_service,
                hermes_config_path=config.recovery_plane.hermes_config_path,
                tlive_config_path=config.recovery_plane.tlive_config_path,
            ),
            hermes_liveness=heartbeat.ok,
            tlive_liveness=probe_tlive_runtime(),
        )
        recovery_available = recovery.available
        expected_chats = tuple(
            item.telegram_chat_id for item in config.projects if item.telegram_chat_id is not None
        )
        hermes_policy = probe_hermes_group_policy(expected_chats)
        checks.extend(
            (
                Check(
                    "recovery:hermes",
                    recovery.hermes_ok,
                    recovery.details["hermes"],
                    required=False,
                ),
                Check(
                    "recovery:tlive",
                    recovery.tlive_ok,
                    recovery.details["tlive"],
                    required=False,
                ),
                Check(
                    "hermes:telegram_group_policy",
                    hermes_policy.ok,
                    hermes_policy.detail,
                    required=False,
                ),
                Check(
                    "hermes:gateway_heartbeat",
                    heartbeat.ok,
                    heartbeat.detail,
                    required=False,
                ),
            )
        )

    healthy = all(check.ok for check in checks if check.required)
    return {
        "ok": healthy,
        "recovery_available": recovery_available,
        "checks": [asdict(check) for check in checks],
    }
