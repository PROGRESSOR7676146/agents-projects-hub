from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .registry import RegistryError


SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-") or "topic"


def terminal_session_name(project: str, topic: str, agent: str, thread_id: int) -> str:
    stem = f"hph-{_slug(project)}-{_slug(topic)}-{_slug(agent)}-{thread_id}"
    return stem[:64].rstrip("-")


def build_codex_remote_argv(
    *,
    socket_path: Path,
    thread_id: str,
    cwd: Path,
) -> tuple[str, ...]:
    socket_path = socket_path.expanduser().resolve()
    cwd = cwd.expanduser().resolve(strict=True)
    if not socket_path.is_absolute():
        raise RegistryError("Codex app-server socket path must be absolute")
    if not SESSION_ID.fullmatch(thread_id):
        raise RegistryError("invalid Codex session id")
    return (
        "codex",
        "resume",
        thread_id,
        "--remote",
        f"unix://{socket_path}",
        "-C",
        str(cwd),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "on-request",
    )


def build_codex_resume_argv(*, thread_id: str, cwd: Path) -> tuple[str, ...]:
    cwd = cwd.expanduser().resolve(strict=True)
    if not SESSION_ID.fullmatch(thread_id):
        raise RegistryError("invalid Codex session id")
    return (
        "codex",
        "resume",
        thread_id,
        "-C",
        str(cwd),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "on-request",
    )
