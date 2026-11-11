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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

WRITER_LOCK_NAME = ".hub-writer.lock"
_ROUTE_KEYS = frozenset({"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"})


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
    if not values["ANTHROPIC_BASE_URL"].startswith(("http://127.0.0.1", "http://localhost")):
        raise ValueError("the Claude route must stay on loopback")
    return values


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
    try:
        state = HubState.open_existing(
            config.state_path, codex_permission_profile=config.codex_permission_profile
        )
    except (StateError, OSError) as error:
        return _fail(f"Hub state is unavailable ({type(error).__name__})")
    try:
        session = state.get_session(args.session_id)
        agent = config.require_agent(session.agent_id)
        if agent.runtime != "claude" or session.writer_mode != "local":
            return _fail("this Hub session is not handed to the local CLI; send /local first")
        if not session.provider_session_id:
            return _fail("this Hub session has no Claude conversation yet")
        topic = state.get_topic(session.topic_id)
        registry = load_registry(config.registry_path)
        root = resolve_topic_execution_root(state, registry, topic)
    except (StateError, ExecutionRootError, KeyError, ValueError) as error:
        return _fail(f"the session could not be verified ({type(error).__name__})")
    finally:
        state.close()

    # Host session directories are keyed by the native Claude session id.
    home = permissions.provider_home / session.provider_session_id
    executable = Path(sys.prefix).parent / "bin" / "claude"
    try:
        if home.is_symlink() or not home.is_dir() or home.stat().st_uid != os.geteuid():
            raise ValueError("unsafe Claude session directory")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the release Claude CLI is missing")
        validate_host_route_settings(root, home)
        route = read_route_environment(permissions.route_environment)
        lock = acquire_writer_lock(home)
    except ClaudeSessionBusy:
        return _fail("the session is busy: a Hub turn or another local CLI is running")
    except (OSError, ValueError) as error:
        return _fail(str(error) if isinstance(error, ValueError) else type(error).__name__)
    # The lock descriptor stays open through exec and in every CLI child, so a
    # Hub turn cannot start until the whole local session has exited.
    os.set_inheritable(lock, True)
    argv = [
        str(executable),
        "--resume",
        session.provider_session_id,
        "--permission-mode",
        "bypassPermissions",
    ]
    if session.model and session.model != "default":
        argv += ["--model", session.model]
    os.chdir(root)
    os.execve(str(executable), argv, local_environment(dict(os.environ), route, home))
    return 1  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())
