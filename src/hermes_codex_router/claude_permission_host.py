"""Worker-owned per-turn socket, not a daemon or a second conversation writer."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .claude_bash_grants import BASH_GRANTS
from .claude_file_policy import validate_file_tool_input
from .claude_file_sandbox import FileToolSandboxConfig, FileToolSandboxError
from .claude_permission_protocol import (
    canonical_json,
    canonical_uuid,
    event_digest,
    parse_json_strict,
)
from .claude_permission_wait import PermissionWaitClock
from .claude_permissions_journal import (
    ClaudePermissionJournal,
    ClaudeSessionModeError,
    PermissionLaunch,
)
from .claude_tool_profiles import profile_tools
from .diagnostic_log import survived
from .external_runtime import ProviderUnavailableError
from .hub_config import AgentDefinition, HubConfig
from .process_namespace import TrustScanLimitError
from .state import HubState, ProviderJobRecord, StateError
from .tlive_permissions import ProtectedTliveClient, TliveCapability, load_tlive_permission_config
from .unix_peer import PeerCredentialError, require_same_uid_peer
from .worker_execution import resolve_external_worker_target, revalidate_worker_execution_root


@dataclass(frozen=True, slots=True)
class HostedClaudeLaunch:
    sandbox: FileToolSandboxConfig
    permission_wait: PermissionWaitClock = field(default_factory=PermissionWaitClock)


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
        self.permission_wait = PermissionWaitClock()
        self.stop = threading.Event()
        self.inflight: threading.Thread | None = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        path.chmod(0o600)
        self.listener.listen(8)
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
            or event.get("tool_name") not in profile_tools(self.launch.profile)
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
        validate_file_tool_input(
            event["tool_name"], event["tool_input"], self.launch.root, profile=self.launch.profile
        )
        job = state.get_provider_job(self.launch.job_id)
        target = revalidate_worker_execution_root(
            state, resolve_external_worker_target(self.config, state, job)
        )
        if Path(target.project.root) != self.launch.root:
            raise StateError("Claude permission registry root changed")
        journal = ClaudePermissionJournal(state)
        capability = self.capability
        scope = None
        granted = False
        if self.launch.profile == "workstation" and event["tool_name"] == "Bash":
            # The owner preauthorized validated foreground Bash for this profile
            # (ADR 0081), as Codex workspace-write runs commands without a card.
            granted = True
        elif self.launch.profile == "developer" and event["tool_name"] == "Bash":
            # A fresh authenticated handshake is mandatory even for cached consent.
            # A restarted/unreachable tlive cannot inherit an earlier grant.
            capability = self.client.hello()
            scope = (
                str(self.config.state_path),
                self.client.config,
                str(self.launch.root),
                self.launch.session_id,
                self.launch.generation,
                job.lease_owner,
            )
            granted = BASH_GRANTS.allows(scope, capability.epoch)
        options = {"notify_wait": False} if granted else {}
        payload = journal.prepare(
            self.launch,
            nonce,
            request["eventDigest"],
            event["tool_name"],
            event["tool_input"],
            **options,
        )
        receipt = None
        if not granted:
            # A live journaled request starts wait accounting; same-UID callers
            # can forge hook bindings. The runner caps total credit per launch.
            with self.permission_wait.waiting():
                receipt = self.client.request(payload, capability, cancellation)
        decision = (
            "allow"
            if receipt is None or receipt.decision == "allow_bash_until_restart"
            else receipt.decision
        )
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
        journal.consume(self.launch, payload, decision)
        if receipt is not None and receipt.decision == "allow_bash_until_restart":
            if scope is None or cancellation.is_set():
                raise StateError("Claude Bash grant binding changed")
            # Publish process-memory authority only after the exact human decision
            # passes the current binding checks and commits successfully.
            BASH_GRANTS.remember(scope, capability.epoch)
        return decision

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                require_same_uid_peer(connection)
            except PeerCredentialError:
                connection.close()  # Reject before request bytes or SQLite are touched.
                continue
            if self.launch.profile == "workstation":
                # Parallel native calls queue here and are answered in turn:
                # Bash needs no human (ADR 0081); a rarer prompted request
                # still has one card at a time, and each hook keeps its deadline.
                while (
                    not self.stop.is_set()
                    and self.inflight is not None
                    and self.inflight.is_alive()
                ):
                    self.inflight.join(0.2)
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
            state = HubState.open(
                self.config.state_path,
                codex_permission_profile=self.config.codex_permission_profile,
            )
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


def _text_session_store(root: Path) -> Path:
    store = Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    return store if store.is_absolute() else root / store


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
            home=_text_session_store(root)
            if settings is None
            else settings.provider_home / session_id,
            is_new=is_new,
            profile="editor" if settings is None else settings.profile,
        )
    except ClaudeSessionModeError:
        raise ProviderUnavailableError(
            "claude_session_mode_changed",
            "Claude's configured access profile or session storage no longer matches this saved "
            "conversation. No model work was started; the conversation was preserved. Restore "
            "the previous configuration to resume it, or deliberately use /new to start a "
            "separate conversation with the changed settings.",
        ) from None
    except StateError:
        raise ProviderUnavailableError(
            "claude_permission_host_unverified",
            "Claude execution binding changed before invocation. The productive turn was not started.",
        ) from None
    if settings is None:
        yield None
        return
    refusal: str | None = None
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
                or home_base.stat().st_uid != os.geteuid()
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
            launch = journal.open_launch(
                job.job_id, token, session_id, root, profile=settings.profile
            )
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
                        settings.tlive_home,
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
                profile=settings.profile,
                toolchain=settings.toolchain,
            )
            with sandbox.wrap((str(Path(executable).resolve()), "--help"), {}, root):
                pass  # Validate and release source pins; invocation acquires its own set.
        except Exception as error:
            if server is not None:
                server.close()
            if launch is not None:
                journal.close_launch(launch)
            # Only the step's class reaches the process log; its text can hold paths.
            survived("claude_permission_host.boundary_unverified", error)
            if isinstance(error, TrustScanLimitError):
                refusal = (
                    "A directory tree the Claude file-tool boundary must inspect has more "
                    "entries than the trust scan allows, for example a project or its Git "
                    "metadata holding large caches. The productive turn was not started."
                )
            else:
                refusal = "Claude file-tool permission boundary could not be verified. The productive turn was not started."
        else:
            server.thread.start()
            try:
                yield HostedClaudeLaunch(sandbox, server.permission_wait)
            finally:
                try:
                    server.close()
                finally:
                    journal.close_launch(launch)
    if refusal is not None:
        # Raised outside the handler so the refusal carries no original exception.
        raise ProviderUnavailableError("claude_permission_host_unverified", refusal)
