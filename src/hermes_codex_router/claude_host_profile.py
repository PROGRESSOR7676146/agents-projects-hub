"""Explicit unconfined Claude launch selected by local configuration."""

import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# A host turn has the owner's CLI rights, so it also gets a CLI-like duration:
# long builds and test runs must not be cut at the confined 900 s default.
# The cap only bounds a hung CLI; /stop, no-progress notices and scope cleanup
# remain the owner's controls (ADR 0095).
HOST_TURN_SECONDS = 6 * 3600


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


def validate_host_result(result: subprocess.CompletedProcess[str], session_id: str | None) -> None:
    """Keep failed terminal validation inside the turn's scope cleanup boundary."""
    from .claude_stream import parse_claude_stream

    parse_claude_stream(
        result.stdout,
        expected_session_id=session_id,
        returncode=result.returncode,
        event_policy=host_event_policy,
    )


CLEANUP_TIMEOUT = 10.0
_SCOPE_NAME = re.compile(r"hub-claude-host-[0-9a-f]{12}\.scope\Z")
_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.:@\\-]+\.service\Z")


def owning_service(proc_cgroup: Path = Path("/proc/self/cgroup")) -> str | None:
    """The systemd service whose cgroup holds this worker, if any."""
    try:
        line = next(row for row in proc_cgroup.read_text().splitlines() if row.startswith("0::"))
    except (OSError, StopIteration, UnicodeError):
        return None
    leaf = line.split("::", 1)[1].strip().rsplit("/", 1)[-1]
    if not _SERVICE_NAME.fullmatch(leaf) or leaf.startswith("user@"):
        return None
    return leaf


def _systemctl_environment() -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
        if key in os.environ
    }
    env["LC_ALL"] = "C"
    return env


class ClaudeHostScopes:
    """Turn-local systemd containment and fail-closed cleanup verification."""

    def __init__(
        self,
        unavailable: Callable[[str, str], Exception],
        owner: Callable[[], str | None] = owning_service,
    ) -> None:
        self._unavailable = unavailable
        self._owner = owner
        self._systemd_run: str | None = None
        self._systemctl: str | None = None
        self.pending: set[str] = set()

    def _resolve(self) -> None:
        if self._systemd_run is None or self._systemctl is None:
            launcher, control = shutil.which("systemd-run"), shutil.which("systemctl")
            if (
                not launcher
                or not control
                or not os.path.isabs(launcher)
                or not os.path.isabs(control)
            ):
                raise self._unavailable(
                    "claude_host_unverified", "Claude host requires systemd scope tools."
                )
            self._systemd_run, self._systemctl = launcher, control

    def _inactive(self, unit: str, timeout: float = 1.0) -> bool:
        assert self._systemctl is not None and _SCOPE_NAME.fullmatch(unit)
        try:
            result = subprocess.run(
                [self._systemctl, "--user", "show", "-p", "ActiveState", "--value", "--", unit],
                capture_output=True,
                text=True,
                timeout=max(timeout, 0.01),
                env=_systemctl_environment(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            return False
        if result.returncode == 0:
            return result.stdout.strip() in {"", "inactive"}
        return (
            result.stderr.strip() == f"Unit {unit} could not be found."
            and not result.stdout.strip()
        )

    def _cleanup(self, unit: str) -> bool:
        assert self._systemctl is not None and _SCOPE_NAME.fullmatch(unit)
        deadline = time.monotonic() + CLEANUP_TIMEOUT
        try:
            subprocess.run(
                [
                    self._systemctl,
                    "--user",
                    "kill",
                    "--signal=SIGKILL",
                    "--kill-whom=all",
                    "--",
                    unit,
                ],
                capture_output=True,
                text=True,
                timeout=max(min(CLEANUP_TIMEOUT, 1.0), 0.01),
                env=_systemctl_environment(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # Only the subsequent state observation can establish cleanup.
        while True:
            remaining = deadline - time.monotonic()
            if self._inactive(unit, min(1.0, max(remaining, 0.01))):
                return True
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))

    @contextmanager
    def turn(
        self, environment: Mapping[str, str]
    ) -> Iterator[Callable[[tuple[str, ...]], tuple[str, ...]]]:
        if self.pending or environment.get("INVOCATION_ID"):
            self._resolve()
        self.pending.difference_update(unit for unit in tuple(self.pending) if self._inactive(unit))
        if self.pending:
            raise self._unavailable(
                "claude_host_cleanup_pending",
                "A previous Claude host scope is still active or unverified.",
            )
        if not environment.get("INVOCATION_ID"):
            yield lambda argv: argv
            return
        unit = f"hub-claude-host-{uuid.uuid4().hex[:12]}.scope"
        owner = self._owner()
        if not _SCOPE_NAME.fullmatch(unit) or owner is None:
            raise self._unavailable(
                "claude_host_unverified", "Claude host scope cannot be bound to its worker."
            )
        assert self._systemd_run is not None

        def wrap(argv: tuple[str, ...]) -> tuple[str, ...]:
            assert self._systemd_run is not None
            return (
                self._systemd_run,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                # The turn ends with its worker: stop, crash or restart stops the scope.
                f"--property=BindsTo={owner}",
                f"--property=After={owner}",
                "--",
                *argv,
            )

        try:
            yield wrap
        except BaseException:
            try:
                cleaned = self._cleanup(unit)
            except BaseException as error:
                from .diagnostic_log import survived

                self.pending.add(unit)
                survived("claude_host_scope.cleanup", error)
            else:
                if not cleaned:
                    from .diagnostic_log import survived

                    self.pending.add(unit)
                    survived("claude_host_scope.cleanup", RuntimeError("scope cleanup unverified"))
            raise
