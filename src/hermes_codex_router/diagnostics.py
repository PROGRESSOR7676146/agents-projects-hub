from __future__ import annotations

import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .hub_config import HubConfig
from .migrations import LATEST_SCHEMA_VERSION
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


def _socket_check(path: Path) -> Check:
    if not path.exists():
        return Check("codex_socket", False, f"missing: {path}")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        return Check("codex_socket", False, str(exc))
    return Check("codex_socket", stat.S_ISSOCK(mode), str(path))


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
    checks.append(_socket_check(config.codex_socket_path))
    checks.append(
        Check(
            "hermes_state_environment",
            os.getenv("HERMES_PROJECT_HUB_STATE") in {None, str(config.state_path)},
            os.getenv("HERMES_PROJECT_HUB_STATE", "not set in this process"),
            required=False,
        )
    )

    healthy = all(check.ok for check in checks if check.required)
    return {"ok": healthy, "checks": [asdict(check) for check in checks]}
