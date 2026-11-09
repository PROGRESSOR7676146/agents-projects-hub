"""Explicit unconfined Claude launch selected by local configuration."""

import os
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ClaudeHostConfig:
    project_root: Path
    provider_home: Path
    claude_executable: Path
    profile: str = "host"


def host_event_policy(event: dict[str, object]) -> None:
    """Keep stream parsing and bounds without rejecting native tool kinds."""


def validate_host_route_settings(root: Path, session_home: Path) -> None:
    """Reject settings that can select credentials while retaining other customizations."""
    import json

    candidates = [session_home / "settings.json", Path("/etc/claude-code/managed-settings.json")]
    for directory in (root, *root.parents):
        candidates.extend(
            (directory / ".claude/settings.json", directory / ".claude/settings.local.json")
        )
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as error:
            raise ValueError("Claude settings could not be verified") from error
        if not isinstance(settings, dict):
            raise ValueError("Claude settings could not be verified")
        env = settings.get("env", {})
        if not isinstance(env, dict) or "apiKeyHelper" in settings:
            raise ValueError("Claude settings can select credentials")
        if any(
            isinstance(key, str)
            and (
                key in {"HOME", "CLAUDE_CONFIG_DIR", "GOOGLE_APPLICATION_CREDENTIALS"}
                or key.startswith("ANTHROPIC_")
                or key.startswith("CLAUDE_CODE_USE_")
                or key.startswith("CLAUDE_CODE_OAUTH_TOKEN")
                or key.startswith(("AWS_", "AZURE_", "GOOGLE_CLOUD_"))
            )
            for key in env
        ):
            raise ValueError("Claude settings can select credentials")


def reap_cgroup_strays(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    environ: Mapping[str, str] = os.environ,
    own_pid: int | None = None,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    grace: float = 2.0,
) -> int:
    """Stop processes a stopped host turn left in this worker's service cgroup.

    The native CLI starts commands in their own process group, so stopping the
    CLI does not reach them. Each worker unit runs one turn at a time, so every
    other process in its own service cgroup belongs to that turn. Outside a
    systemd service this does nothing.
    """
    if not environ.get("INVOCATION_ID"):
        return 0
    try:
        line = next(row for row in proc_cgroup.read_text().splitlines() if row.startswith("0::"))
        relative = line.split("::", 1)[1].strip()
        if not relative.endswith(".service") or relative.rsplit("/", 1)[-1].startswith("user@"):
            return 0
        procs = cgroup_root / relative.lstrip("/") / "cgroup.procs"
        own = os.getpid() if own_pid is None else own_pid

        def strays() -> set[int]:
            return {int(pid) for pid in procs.read_text().split()} - {own}

        targets = strays()
    except (OSError, StopIteration, ValueError):
        return 0
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        for pid in targets:
            try:
                kill(pid, signal_number)
            except (ProcessLookupError, PermissionError):
                pass
        sleep(grace)
        try:
            targets = strays()
        except (OSError, ValueError):
            return len(targets)
        if not targets:
            break
    return len(targets)
