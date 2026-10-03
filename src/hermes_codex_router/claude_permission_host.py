"""Worker-owned per-turn socket, not a daemon or a second conversation writer."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .claude_file_policy import validate_file_tool_input
from .claude_file_sandbox import FileToolSandboxConfig, FileToolSandboxError
from .claude_permission_protocol import (
    canonical_json,
    canonical_uuid,
    event_digest,
    parse_json_strict,
)
from .claude_permissions_journal import FILE_TOOLS, ClaudePermissionJournal, PermissionLaunch
from .external_runtime import ProviderUnavailableError
from .hub_config import AgentDefinition, HubConfig
from .state import HubState, ProviderJobRecord, StateError
from .tlive_permissions import ProtectedTliveClient, TliveCapability, load_tlive_permission_config
from .worker_execution import resolve_external_worker_target, revalidate_worker_execution_root


@dataclass(frozen=True, slots=True)
class HostedClaudeLaunch:
    sandbox: FileToolSandboxConfig


class _HookWait(threading.Event):
    """Cancel human waiting when the native requester leaves, without another thread."""

    def __init__(self, connection: socket.socket, stopped: threading.Event) -> None:
        super().__init__()
        connection.setblocking(False)
        self.connection, self.stopped = connection, stopped

    def is_set(self) -> bool:
        if self.stopped.is_set():
            return True
        try:
            self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except BlockingIOError:
            return False
        except OSError:
            return True
        return True  # EOF or unexpected additional request bytes both cancel.


class PermissionServer:
    def __init__(
        self,
        path: Path,
        config: HubConfig,
        launch: PermissionLaunch,
        client: ProtectedTliveClient,
        capability: TliveCapability,
    ) -> None:
        self.path, self.config, self.launch = path, config, launch
        self.client, self.capability = client, capability
        self.stop = threading.Event()
        self.inflight: threading.Thread | None = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        path.chmod(0o600)
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(
            target=self._serve, name="claude-permission-host", daemon=True
        )

    def _read(self, connection: socket.socket) -> dict[str, Any]:
        deadline = time.monotonic() + 3
        data = bytearray()
        connection.settimeout(0.2)
        while not self.stop.is_set() and time.monotonic() < deadline:
            try:
                chunk = connection.recv(min(4096, 131073 - len(data)))
            except socket.timeout:
                continue
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 131072:
                break
            if b"\n" in data:
                line, trailing = bytes(data).split(b"\n", 1)
                value = parse_json_strict(line)
                if not trailing and isinstance(value, dict):
                    return value
                break
        raise ValueError("invalid native permission request")

    def _decision(
        self, state: HubState, request: dict[str, Any], cancel: threading.Event | None = None
    ) -> str:
        cancellation = self.stop if cancel is None else cancel
        if cancellation.is_set():
            raise StateError("native permission hook disconnected")
        if (
            set(request) != {"kind", "version", "nonce", "event", "eventDigest"}
            or request["kind"] != "claude.permission.request"
            or type(request["version"]) is not int
            or request["version"] != 1
        ):
            raise ValueError("invalid native permission envelope")
        nonce = canonical_uuid(request["nonce"])
        event = request["event"]
        if not isinstance(event, dict) or event_digest(event) != request["eventDigest"]:
            raise ValueError("invalid native permission digest")
        if (
            event.get("hook_event_name") != "PermissionRequest"
            or event.get("session_id") != self.launch.session_id
            or event.get("cwd") != str(self.launch.root)
            or event.get("tool_name") not in FILE_TOOLS
            or not isinstance(event.get("tool_input"), dict)
        ):
            raise ValueError("native permission binding changed")
        if any(
            key in event
            for key in (
                "agent_id",
                "agentId",
                "parent_tool_use_id",
                "updatedPermissions",
                "permission_updates",
            )
        ):
            raise ValueError("unsupported native permission authority")
        validate_file_tool_input(event["tool_name"], event["tool_input"], self.launch.root)
        job = state.get_provider_job(self.launch.job_id)
        target = revalidate_worker_execution_root(
            state, resolve_external_worker_target(self.config, state, job)
        )
        if Path(target.project.root) != self.launch.root:
            raise StateError("Claude permission registry root changed")
        journal = ClaudePermissionJournal(state)
        payload = journal.prepare(
            self.launch, nonce, request["eventDigest"], event["tool_name"], event["tool_input"]
        )
        receipt = self.client.request(payload, self.capability, cancellation)
        if cancellation.is_set():
            raise StateError("Claude permission host was stopped")
        # Registry is a filesystem source; recheck after the human wait too.
        target = revalidate_worker_execution_root(
            state,
            resolve_external_worker_target(
                self.config, state, state.get_provider_job(self.launch.job_id)
            ),
        )
        if Path(target.project.root) != self.launch.root:
            raise StateError("Claude permission registry root changed")
        journal.consume(self.launch, payload, receipt.decision)
        return receipt.decision

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.stop.is_set() or self.inflight is not None and self.inflight.is_alive():
                connection.close()  # No queue: another hook gets native Deny immediately.
                continue
            self.inflight = threading.Thread(
                target=self._serve_connection,
                args=(connection,),
                name="claude-permission-wait",
                daemon=True,
            )
            self.inflight.start()

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            state = HubState.open(self.config.state_path)
        except Exception:
            connection.close()  # Hook emits fixed Deny on EOF; no raw DB diagnostic escapes.
            return
        try:
            with connection:
                request: dict[str, Any] = {}
                decision = "deny"
                try:
                    request = self._read(connection)
                    decision = self._decision(state, request, _HookWait(connection, self.stop))
                except Exception:
                    # Do not send raw input, tool output, secrets or OS diagnostics.
                    decision = "deny"
                    try:
                        ClaudePermissionJournal(state).revoke_request(
                            self.launch, canonical_uuid(request.get("nonce"))
                        )
                    except Exception:
                        decision = "deny"  # persistence failure still denies native operation
                try:
                    nonce = canonical_uuid(request.get("nonce"))
                except ValueError:
                    nonce = None
                digest = request.get("eventDigest")
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                ):
                    digest = None
                reply = {
                    "kind": "claude.permission.result",
                    "version": 1,
                    "nonce": nonce,
                    "eventDigest": digest,
                    "decision": decision,
                }
                try:
                    connection.sendall((canonical_json(reply) + "\n").encode("utf-8"))
                except OSError:
                    pass  # consumed Allow stays consumed even if its reply is lost
        finally:
            connection.close()
            state.close()

    def close(self) -> None:
        self.stop.set()
        self.listener.close()
        if self.thread.ident is not None:
            self.thread.join(timeout=1)
        if self.inflight is not None:
            self.inflight.join(timeout=3)
        if self.thread.is_alive() or self.inflight is not None and self.inflight.is_alive():
            raise StateError("Claude permission host cleanup is unconfirmed")


@contextmanager
def hosted_claude_launch(
    config: HubConfig,
    state: HubState,
    agent: AgentDefinition,
    job: ProviderJobRecord,
    token: str,
    session_id: str | None,
    root: Path,
    *,
    is_new: bool = False,
) -> Iterator[HostedClaudeLaunch | None]:
    settings = config.claude_file_permissions
    if agent.runtime != "claude":
        yield None
        return
    server: PermissionServer | None = None
    launch: PermissionLaunch | None = None
    journal = ClaudePermissionJournal(state)
    try:
        if session_id is None:
            raise StateError("Claude native identity is missing")
        journal.bind_session_mode(
            job.job_id,
            token,
            session_id,
            root,
            mode="text_only" if settings is None else "file_tools",
            home=Path.home() if settings is None else settings.provider_home / session_id,
            is_new=is_new,
        )
    except StateError:
        raise ProviderUnavailableError(
            "claude_session_mode_changed",
            "Claude session mode or session storage changed. The productive turn was not started; use /new before changing mode.",
        ) from None
    if settings is None:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="hub-claude-permission-") as directory:
        try:
            if session_id is None:
                raise ValueError("Claude native identity is missing")
            transport = load_tlive_permission_config(settings.tlive_config)
            if (
                int(transport.owner_id) not in config.owner_user_ids
                or transport.chat_id != transport.owner_id
            ):
                raise ValueError("protected tlive owner is not a Hub owner")
            client = ProtectedTliveClient(transport)
            capability = client.hello()
            executable = shutil.which(agent.executable or "claude")
            if executable is None:
                raise FileToolSandboxError("Claude executable is unavailable")
            home_base = settings.provider_home
            if (
                home_base.is_symlink()
                or not home_base.is_dir()
                or home_base.resolve() != home_base
                or home_base.stat().st_uid != os.getuid()
                or home_base.stat().st_mode & 0o077
            ):
                raise FileToolSandboxError("provider session home is unsafe")
            home = home_base / session_id
            home.mkdir(mode=0o700, exist_ok=True)
            if home.is_symlink():
                raise FileToolSandboxError("provider session home is unsafe")
            # Never import customizations persisted by an earlier provider run.
            for relative in (
                ".claude/settings.json",
                ".claude/settings.local.json",
                ".claude/plugins",
                ".claude/hooks",
                ".claude/agents",
                ".claude/commands",
                ".claude/skills",
            ):
                if (home / relative).exists() or (home / relative).is_symlink():
                    raise FileToolSandboxError("provider session home has customizations")
            launch = journal.open_launch(job.job_id, token, session_id, root)
            path = Path(directory) / "permission.sock"
            server = PermissionServer(path, config, launch, client, capability)
            sandbox = FileToolSandboxConfig(
                settings.bwrap_executable,
                root,
                home,
                settings.runtime_roots,
                Path(executable).resolve(),
                settings.python_executable,
                settings.hook_code_root,
                path,
                tuple(
                    {
                        *settings.private_paths,
                        config.state_path.parent,
                        settings.tlive_config,
                        Path(transport.socket_path),
                        config.registry_path,
                        *(
                            definition.token_file
                            for definition in config.agents
                            if definition.token_file
                        ),
                        *([config.hub_bot.token_file] if config.hub_bot else []),
                    }
                ),
            )
            sandbox.wrap((str(Path(executable).resolve()), "--help"), {}, root)
        except Exception:
            if server is not None:
                server.close()
            if launch is not None:
                journal.close_launch(launch)
            raise ProviderUnavailableError(
                "claude_permission_host_unverified",
                "Claude file-tool permission boundary could not be verified. The productive turn was not started.",
            ) from None
        server.thread.start()
        try:
            yield HostedClaudeLaunch(sandbox)
        finally:
            try:
                server.close()
            finally:
                journal.close_launch(launch)
