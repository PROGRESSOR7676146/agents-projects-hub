"""Contain the offline native fixture independently of provider permissions."""

from __future__ import annotations

import os
from pathlib import Path

ACTOR_PATH = "/opt/example-native/actor.py"


def native_namespace_environment() -> dict[str, str]:
    return {
        "HOME": "/home/example",
        "CODEX_HOME": "/home/example/.codex",
        "XDG_CONFIG_HOME": "/home/example/.config",
        "XDG_CACHE_HOME": "/home/example/.cache",
        "XDG_DATA_HOME": "/home/example/.local/share",
        "XDG_STATE_HOME": "/home/example/.local/state",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "RUST_LOG": "error",
    }


def _runtime_mounts() -> list[str]:
    argv: list[str] = []
    for source in ("/usr/bin", "/usr/lib", "/usr/lib64"):
        if Path(source).is_dir():
            argv.extend(("--ro-bind", source, source))
        elif source != "/usr/lib64":
            raise RuntimeError("offline native system runtime is unavailable")
    for source in ("/bin", "/lib", "/lib64"):
        path = Path(source)
        if path.is_symlink():
            target = path.readlink()
            resolved = path.resolve(strict=True)
            if not any(
                resolved.is_relative_to(root)
                for root in (Path("/usr/bin"), Path("/usr/lib"), Path("/usr/lib64"))
            ):
                raise RuntimeError("offline native runtime link is unsupported")
            argv.extend(("--symlink", str(target), source))
        elif path.is_dir():
            argv.extend(("--ro-bind", source, source))
        elif source != "/lib64":
            raise RuntimeError("offline native system runtime is unavailable")
    return argv


def native_namespace_argv(
    *,
    binary: Path,
    actor: Path,
    requirements: Path,
    project: Path,
    authority: Path,
    mcp_server: Path | None = None,
    notification_listener: bool = False,
    listener_directory: Path | None = None,
) -> list[str]:
    if notification_listener and mcp_server is not None:
        raise ValueError("notification fixture excludes MCP")
    if notification_listener != (listener_directory is not None):
        raise ValueError("notification fixture requires disposable endpoint storage")
    return [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        *_runtime_mounts(),
        "--tmpfs",
        "/tmp",
        *(
            ["--bind", str(listener_directory), f"/tmp/codex-daemon-{os.getuid()}"]
            if listener_directory is not None
            else []
        ),
        "--dir",
        "/home/example/.codex",
        "--dir",
        "/home/example/.config",
        "--dir",
        "/home/example/.cache",
        "--dir",
        "/home/example/.local/share",
        "--dir",
        "/home/example/.local/state",
        "--dir",
        str(project.parent),
        "--bind",
        str(project),
        str(project),
        "--bind",
        str(authority),
        str(authority),
        "--ro-bind",
        str(actor),
        ACTOR_PATH,
        *(
            ["--ro-bind", str(mcp_server), "/opt/example-native/mcp_server.py"]
            if mcp_server is not None
            else []
        ),
        "--ro-bind",
        str(requirements),
        "/etc/codex/requirements.toml",
        "--ro-bind",
        str(binary),
        "/usr/local/bin/example-codex",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        str(project),
        "--",
        "/usr/bin/python3",
        "-I",
        ACTOR_PATH,
        *(["--mcp"] if mcp_server is not None else []),
        *(["--notifications"] if notification_listener else []),
    ]
