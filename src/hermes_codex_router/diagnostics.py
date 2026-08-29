from __future__ import annotations

import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .hub_config import HubConfig
from .migrations import LATEST_SCHEMA_VERSION
from .recovery_plane import RecoveryPlaneProbe, probe_recovery_plane
from .registry import load_registry
from .state import HubState
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
        finally:
            state.close()
    except Exception as exc:
        checks.append(Check("state", False, str(exc)))

    checks.extend([_command("git"), _command("codex"), _command("tmux")])
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
        recovery = probe_recovery_plane(
            RecoveryPlaneProbe(
                hermes_service=config.recovery_plane.hermes_service,
                tlive_service=config.recovery_plane.tlive_service,
                hermes_config_path=config.recovery_plane.hermes_config_path,
                tlive_config_path=config.recovery_plane.tlive_config_path,
            )
        )
        recovery_available = recovery.available
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
            )
        )

    healthy = all(check.ok for check in checks if check.required)
    return {
        "ok": healthy,
        "recovery_available": recovery_available,
        "checks": [asdict(check) for check in checks],
    }
