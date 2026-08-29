from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


class LocalTransferError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalResumeCommand:
    argv: tuple[str, ...]
    cwd: str | None = None

    @property
    def display(self) -> str:
        command = shlex.join(self.argv)
        if self.cwd is None:
            return command
        return f"cd -- {shlex.quote(self.cwd)} && {command}"


def local_resume_command(
    runtime: str,
    executable: str | None,
    provider_session_id: str,
    project_root: Path,
) -> LocalResumeCommand:
    session_id = provider_session_id.strip()
    if not session_id:
        raise LocalTransferError("provider session id is empty")
    root = str(project_root.expanduser().resolve(strict=True))
    if runtime == "codex":
        argv = ("codex", "resume", session_id, "-C", root)
    elif runtime == "opencode":
        argv = (executable or "opencode", root, "--session", session_id)
    elif runtime == "antigravity":
        argv = (
            executable or "agy",
            "--conversation",
            session_id,
            "--sandbox",
            "--mode",
            "accept-edits",
        )
    else:
        raise LocalTransferError(f"local resume is not supported for runtime: {runtime}")
    return LocalResumeCommand(argv, root if runtime == "antigravity" else None)
