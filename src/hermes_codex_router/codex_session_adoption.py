"""Explicit local CLI-thread attachment; no Telegram or productive provider calls."""

from __future__ import annotations

import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .codex_appserver import (
    CodexAppServerClient,
    CodexMetadataError,
    CodexThreadMetadata,
    RpcError,
    StdioJsonLineTransport,
    UnixWebSocketTransport,
    validate_codex_thread_id,
)
from .hub_config import HubConfig
from .migrations import LATEST_SCHEMA_VERSION
from .provider_catalog_cache import ProviderCatalogCache
from .registry import RegistryError, load_registry
from .session_adoption_policy import supports_adoption
from .session_adoption_state import AdoptionRequest, AdoptionTarget, CodexSessionOrigins
from .state import HubState, StateError


class AdoptionError(ValueError):
    def __init__(self, reason: str, *, temporary: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = 3 if temporary else 2


@contextmanager
def open_adoption_state(path: Path, *, writable: bool = False) -> Iterator[HubState]:
    """Open an existing private database without migration, chmod or creation."""
    connection = None
    try:
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise AdoptionError("state_unavailable")
        connection = sqlite3.connect(
            path.resolve(strict=True).as_uri() + ("?mode=rw" if writable else "?mode=ro"),
            uri=True,
            timeout=5,
        )
        if connection.execute("PRAGMA user_version").fetchone()[0] != LATEST_SCHEMA_VERSION:
            raise AdoptionError("schema_upgrade_required")
        connection.execute("PRAGMA foreign_keys=ON")
        yield HubState(connection)
    except (OSError, sqlite3.Error):
        raise AdoptionError("state_unavailable") from None
    finally:
        if connection is not None:
            connection.close()


def inspect_codex_session(config: HubConfig, thread_id: str, root: Path) -> CodexThreadMetadata:
    """Own only this client/temporary stdio child, never a shared daemon."""
    deadline = time.monotonic() + 10
    use_socket = config.codex_socket_path.is_socket() and (
        not config.manage_codex_server or config.codex_stdio_executable is None
    )
    if use_socket:
        transport = UnixWebSocketTransport(
            config.codex_socket_path, timeout=max(0.01, deadline - time.monotonic())
        )
    elif config.codex_stdio_executable is not None:
        transport = StdioJsonLineTransport.start(str(config.codex_stdio_executable))
    else:
        raise RpcError("configured Codex metadata transport unavailable")
    client = CodexAppServerClient(transport, approval_policy="never")
    try:
        client.initialize(deadline=deadline)
        return client.read_thread_metadata(thread_id=thread_id, cwd=root, deadline=deadline)
    finally:
        client.close()


def _project_root(config: HubConfig, project_id: str, chat_id: int) -> Path:
    try:
        if config.project_for_chat(chat_id).project_id != project_id:
            raise AdoptionError("project_binding_mismatch")
        registry = load_registry(config.registry_path)
        project = registry.require_project(project_id)
        root = project.root.resolve(strict=True)
        if not any(
            root.is_relative_to(allowed.resolve(strict=True)) for allowed in registry.allowed_roots
        ):
            raise AdoptionError("project_root_invalid")
        result = subprocess.run(
            ("git", "-C", str(root), "rev-parse", "--show-toplevel"),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        if Path(result.stdout.strip()).resolve(strict=True) != root:
            raise AdoptionError("project_root_invalid")
        return root
    except AdoptionError:
        raise
    except (KeyError, RegistryError, OSError, ValueError, subprocess.SubprocessError):
        raise AdoptionError("project_root_invalid") from None


def _selection(config: HubConfig, model: str | None, effort: str | None) -> tuple[str, str]:
    agent = config.require_agent("codex")
    selected_model, selected_effort = model or agent.default_model, effort or agent.default_effort
    if selected_model == agent.default_model and selected_effort == agent.default_effort:
        return selected_model, selected_effort
    cached = ProviderCatalogCache(config.state_path.with_name("provider-model-catalogs.json")).load(
        "codex"
    )
    if cached is not None and any(
        item.model_id == selected_model and selected_effort in item.efforts
        for item in cached.models
    ):
        return selected_model, selected_effort
    raise AdoptionError("model_effort_unavailable")


def _result(request: AdoptionRequest, target: AdoptionTarget, action: str) -> dict[str, object]:
    session = target.session
    return {
        "format_version": 1,
        "ok": True,
        "action": action,
        "reason_code": "ok",
        "project_id": request.project_id,
        "chat_id": request.chat_id,
        "thread_id": request.thread_id,
        "codex_thread_id": request.provider_thread_id,
        "hub_session_id": session.session_id if session else None,
        "generation": session.generation if session else None,
        "replaces_session_id": request.replaces_session_id,
        "model": request.model,
        "effort": request.effort,
        "writer_mode": session.writer_mode if session else None,
        "next_action": (
            "continue_in_telegram"
            if target.already_attached and session and session.writer_mode == "telegram"
            else "send_return_in_topic_after_cli_closed"
            if action != "preview"
            else "apply_with_cli_closed_confirmation"
        ),
        "environment_notice": "Hub runtime settings apply; CLI tools/plugins/environment are not guaranteed identical. Histories are not merged.",
    }


def attach_codex_session(
    config: HubConfig,
    *,
    project_id: str,
    chat_id: int,
    thread_id: int,
    codex_thread_id: str,
    model: str | None = None,
    effort: str | None = None,
    replace_session: str | None = None,
    apply: bool = False,
    confirm_cli_closed: bool = False,
    inspector: Callable[[HubConfig, str, Path], CodexThreadMetadata] = inspect_codex_session,
) -> dict[str, object]:
    try:
        validate_codex_thread_id(codex_thread_id)
        if (
            type(chat_id) is not int
            or not -(2**63) <= chat_id < 0
            or type(thread_id) is not int
            or not 0 < thread_id < 2**63
        ):
            raise AdoptionError("invalid_topic_identity")
        if apply and not confirm_cli_closed:
            raise AdoptionError("cli_close_confirmation_required")
        if not supports_adoption(config):
            raise AdoptionError("execution_mode_unsupported")
        root = _project_root(config, project_id, chat_id)
        selected_model, selected_effort = _selection(config, model, effort)
        request = AdoptionRequest(
            project_id,
            chat_id,
            thread_id,
            codex_thread_id,
            root,
            selected_model,
            selected_effort,
            replace_session,
        )
        with open_adoption_state(config.state_path) as state:
            target = CodexSessionOrigins(state).preview(request)
        if target.already_attached:
            return _result(request, target, "already_attached")
        metadata = inspector(config, codex_thread_id, root)
        if (
            metadata.thread_id != codex_thread_id
            or metadata.cwd != root
            or metadata.model_provider != "openai"
            or metadata.status not in ("idle", "notLoaded")
        ):
            raise AdoptionError("source_identity_mismatch")
        if not apply:
            return _result(request, target, "preview")
        if _project_root(config, project_id, chat_id) != root:
            raise AdoptionError("project_root_changed")
        with open_adoption_state(config.state_path, writable=True) as state:
            result = CodexSessionOrigins(state).attach(
                request, expected_session_id=target.session.session_id if target.session else None
            )
        return _result(
            request, result, "already_attached" if result.already_attached else "attached"
        )
    except AdoptionError:
        raise
    except CodexMetadataError as exc:
        raise AdoptionError(str(exc)) from None
    except StateError as exc:
        raise AdoptionError(str(exc)) from None
    except (RpcError, OSError, EOFError, TimeoutError):
        raise AdoptionError("metadata_unavailable", temporary=True) from None
