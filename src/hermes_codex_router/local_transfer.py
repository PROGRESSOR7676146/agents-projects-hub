from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .antigravity_model import model_arguments
from .codex_permissions import MANAGED_LOCAL_REFUSAL

if TYPE_CHECKING:
    from .hub_config import AgentDefinition, HubConfig
    from .state_sessions import SessionRecord

CLAUDE_LOCAL_REFUSAL = (
    "Local hand-off for Claude needs the host profile with a configured route environment."
)


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
    *,
    model_provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    codex_socket_path: Path | None = None,
    permission_profile: str | None = None,
    hub_config: HubConfig | None = None,
    hub_session_id: str | None = None,
) -> LocalResumeCommand:
    if runtime == "codex" and permission_profile is not None:
        raise LocalTransferError(MANAGED_LOCAL_REFUSAL)
    session_id = provider_session_id.strip()
    if not session_id:
        raise LocalTransferError("provider session id is empty")
    root = str(project_root.expanduser().resolve(strict=True))
    if runtime == "codex":
        argv = ("codex", "resume", session_id, "-C", root)
        if codex_socket_path is not None:
            # Attach to the owning daemon; standalone resume opens a second
            # persistence writer even when the Hub's logical lease is local.
            socket = codex_socket_path.expanduser().resolve()
            argv = ("codex", "--remote", f"unix://{socket}", *argv[1:])
        if model_provider is not None:
            if not model:
                raise LocalTransferError("explicit provider resume requires a model")
            argv += (
                "-c",
                "model_provider=" + json.dumps(model_provider),
                "-c",
                "model=" + json.dumps(model),
            )
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
            *model_arguments(model, effort),
        )
    elif runtime == "claude":
        # The native session lives in a Hub session directory reached through
        # the private route; a Hub command opens it without showing secrets.
        permissions = hub_config.claude_file_permissions if hub_config else None
        if (
            hub_config is None
            or hub_config.source_path is None
            or hub_session_id is None
            or permissions is None
            or permissions.profile != "host"
            or permissions.route_environment is None
        ):
            raise LocalTransferError(CLAUDE_LOCAL_REFUSAL)
        argv = (
            sys.executable,
            "-I",
            "-m",
            "hermes_codex_router.claude_local",
            "--config",
            str(hub_config.source_path),
            hub_session_id,
        )
    else:
        raise LocalTransferError(f"local resume is not supported for runtime: {runtime}")
    return LocalResumeCommand(argv, root if runtime == "antigravity" else None)


def session_resume_command(
    config: HubConfig, agent: AgentDefinition, session: SessionRecord, root: Path
) -> LocalResumeCommand:
    """Resume command for one Hub session's native conversation."""
    if not session.provider_session_id:
        raise LocalTransferError("provider session id is empty")
    return local_resume_command(
        agent.runtime,
        agent.executable,
        session.provider_session_id,
        root,
        model_provider=config.codex_model_provider,
        permission_profile=session.codex_permission_profile,
        model=session.model,
        codex_socket_path=config.codex_socket_path,
        effort=session.effort,
        hub_config=config,
        hub_session_id=session.session_id,
    )
