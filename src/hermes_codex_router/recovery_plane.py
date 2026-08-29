from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class RecoveryPlaneProbe:
    hermes_service: str
    tlive_service: str
    hermes_config_path: Path
    tlive_config_path: Path


@dataclass(frozen=True, slots=True)
class RecoveryPlaneStatus:
    hermes_ok: bool
    tlive_ok: bool
    available: bool
    details: dict[str, str]


def _service_active(argv: tuple[str, ...]) -> bool:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _private_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_mode & 0o077 == 0
    except OSError:
        return False


def probe_recovery_plane(
    probe: RecoveryPlaneProbe,
    *,
    service_active: Callable[[tuple[str, ...]], bool] = _service_active,
    command_available: Callable[[str], bool] = lambda command: shutil.which(command) is not None,
) -> RecoveryPlaneStatus:
    hermes_command = command_available("hermes")
    tlive_command = command_available("tlive")
    hermes_config = _private_file(probe.hermes_config_path)
    tlive_config = _private_file(probe.tlive_config_path)
    hermes_service = service_active(
        ("systemctl", "--user", "is-active", "--quiet", probe.hermes_service)
    )
    tlive_service = service_active(
        ("systemctl", "--user", "is-active", "--quiet", probe.tlive_service)
    )
    hermes_ok = hermes_command and hermes_config and hermes_service
    tlive_ok = tlive_command and tlive_config and tlive_service
    return RecoveryPlaneStatus(
        hermes_ok=hermes_ok,
        tlive_ok=tlive_ok,
        available=hermes_ok or tlive_ok,
        details={
            "hermes": (
                f"command={'ok' if hermes_command else 'missing'}, "
                f"config={'private' if hermes_config else 'missing-or-unsafe'}, "
                f"service={'active' if hermes_service else 'inactive'}"
            ),
            "tlive": (
                f"command={'ok' if tlive_command else 'missing'}, "
                f"config={'private' if tlive_config else 'missing-or-unsafe'}, "
                f"service={'active' if tlive_service else 'inactive'}"
            ),
        },
    )
