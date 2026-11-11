"""Hand a Claude `host` session to the owner's local CLI without exposing credentials.

Telegram shows only this module's command. It rechecks that Hub recorded local
ownership, takes the session writer lock that every Hub turn also takes, loads
the private route environment and replaces itself with the interactive CLI on
the same session directory, so no transcript is copied.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

WRITER_LOCK_NAME = ".hub-writer.lock"
_ROUTE_KEYS = frozenset({"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


class ClaudeSessionBusy(RuntimeError):
    """Another writer (a Hub turn or a local CLI) holds the session."""


def acquire_writer_lock(home: Path) -> int:
    """Return a descriptor holding the session's exclusive writer lock."""
    descriptor = os.open(
        home / WRITER_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise ClaudeSessionBusy("the Claude session is open in another writer") from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def session_writer_lock(home: Path) -> Iterator[None]:
    descriptor = acquire_writer_lock(home)
    try:
        yield
    finally:
        os.close(descriptor)


def read_route_environment(path: Path) -> dict[str, str]:
    """Read only the two route keys from a private owner file; never echo values."""
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_size > 16384
    ):
        raise ValueError("the Claude route environment file must be a private owner file")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key in _ROUTE_KEYS:
            values[key] = value.strip().strip("'\"")
    if set(values) != _ROUTE_KEYS or not all(values.values()):
        raise ValueError("the Claude route environment file is incomplete")
    if not _is_loopback_url(values["ANTHROPIC_BASE_URL"]):
        raise ValueError("the Claude route must stay on loopback")
    return values


def _is_loopback_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        return False
    return (
        url.scheme == "http"
        and url.hostname in _LOOPBACK_HOSTS
        and url.username is None
        and url.password is None
        and port is not None
        and not url.query
        and not url.fragment
    )


def host_session_home(provider_home: Path, native_session_id: str, *, create: bool) -> Path:
    """Return a verified private session directory; workers and hand-off share it."""
    if str(uuid.UUID(native_session_id)) != native_session_id:
        raise ValueError("invalid Claude session id")
    if (
        provider_home.is_symlink()
        or not provider_home.is_dir()
        or provider_home.resolve() != provider_home
        or provider_home.stat().st_uid != os.geteuid()
        or provider_home.stat().st_mode & 0o077
    ):
        raise ValueError("unsafe host session storage")
    home = provider_home / native_session_id
    if create:
        home.mkdir(mode=0o700, exist_ok=True)
    if (
        home.is_symlink()
        or not home.is_dir()
        or home.stat().st_uid != os.geteuid()
        or stat.S_IMODE(home.stat().st_mode) != 0o700
    ):
        raise ValueError("unsafe host session storage")
    if (home / ".credentials.json").exists() or (home / ".credentials.json").is_symlink():
        raise ValueError("host session contains native credentials")
    return home


def model_effort_arguments(model: str | None, effort: str | None) -> list[str]:
    """The same model/effort mapping as Hub's Claude turns, or refuse."""
    arguments: list[str] = []
    if model and model not in {"unknown", "default"}:
        arguments += ["--model", model]
    if effort and effort not in {"none", "minimal", "default"}:
        if effort not in _CLAUDE_EFFORTS:
            raise ValueError("the saved Claude effort cannot be applied locally")
        arguments += ["--effort", effort]
    return arguments


def local_environment(base: dict[str, str], route: dict[str, str], home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in base.items()
        if not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_CLOUD_"))
        and key not in {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
    }
    environment.update(route)
    environment["CLAUDE_CONFIG_DIR"] = str(home)
    environment.setdefault("CLAUDE_CODE_PROMPT_CACHE_TTL", "1h")
    return environment


def _fail(message: str) -> int:
    print(f"claude-local: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-local", description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("session_id")
    args = parser.parse_args(argv)

    from .claude_host_profile import validate_host_route_settings
    from .hub_config import HubConfigError, load_hub_config
    from .registry import ExecutionRootError, load_registry
    from .state import HubState, StateError
    from .topic_execution import resolve_topic_execution_root

    try:
        config = load_hub_config(args.config)
    except HubConfigError as error:
        return _fail(f"configuration refused: {error}")
    permissions = config.claude_file_permissions
    if (
        permissions is None
        or permissions.profile != "host"
        or permissions.route_environment is None
    ):
        return _fail("local hand-off needs the Claude host profile with a route environment")

    def verified_binding() -> tuple[str, Path, list[str]]:
        """Read local ownership, root and model/effort from the current Hub state."""
        state = HubState.open_existing(
            config.state_path, codex_permission_profile=config.codex_permission_profile
        )
        try:
            session = state.get_session(args.session_id)
            if config.require_agent(session.agent_id).runtime != "claude":
                raise ValueError("this Hub session is not a Claude session")
            if session.writer_mode != "local":
                raise ValueError(
                    "this Hub session is not handed to the local CLI; send /local first"
                )
            if not session.provider_session_id:
                raise ValueError("this Hub session has no Claude conversation yet")
            topic = state.get_topic(session.topic_id)
            root = resolve_topic_execution_root(state, load_registry(config.registry_path), topic)
            arguments = model_effort_arguments(session.model, session.effort)
            return session.provider_session_id, root, arguments
        finally:
            state.close()

    executable = Path(sys.prefix).parent / "bin" / "claude"
    lock: int | None = None
    try:
        native, root, arguments = verified_binding()
        home = host_session_home(permissions.provider_home, native, create=False)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the release Claude CLI is missing")
        validate_host_route_settings(root, home)
        route = read_route_environment(permissions.route_environment)
        lock = acquire_writer_lock(home)
        # Recheck under the lock: a /return may have completed meanwhile.
        if verified_binding() != (native, root, arguments):
            raise ValueError("the Hub session changed; send /local again")
    except ClaudeSessionBusy:
        return _fail("the session is busy: a Hub turn or another local CLI is running")
    except (StateError, ExecutionRootError, KeyError, OSError, ValueError) as error:
        if lock is not None:
            os.close(lock)
        detail = str(error) if type(error) is ValueError else type(error).__name__
        return _fail(f"the session could not be opened locally ({detail})")
    # The lock descriptor stays open through exec and in every CLI child, so a
    # Hub turn cannot start until the whole local session has exited.
    os.set_inheritable(lock, True)
    argv = [
        str(executable),
        "--resume",
        native,
        "--permission-mode",
        "bypassPermissions",
        *arguments,
    ]
    os.chdir(root)
    os.execve(str(executable), argv, local_environment(dict(os.environ), route, home))
    return 1  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())
