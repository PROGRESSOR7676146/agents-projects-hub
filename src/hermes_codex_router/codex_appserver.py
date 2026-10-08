from __future__ import annotations

import re
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol, Sequence, cast

from .codex_activity import (
    CodexActivityEvent,
    normalize_codex_activity,
)
from .codex_activity_requests import (
    MAX_PENDING_ACTIVITY,
    ActivityObservationUnavailable,
    CodexActivityRequests,
)
from .codex_connection_completion import CompletedConnectionProof
from .codex_failure import (
    MAX_PARTIAL_TEXT,
    UnsupportedCodexPermissionProfileError,
    codex_failure_reason,
)
from .codex_notifications import retain_turn_notification
from .codex_permissions import (
    CodexPermissionBinding,
    CodexPermissionPolicyDriftError,
    CodexPermissionProfileError,
    validate_permission_profile_id,
    verify_managed_selection,
)
from .codex_response_drain import CodexResponseDrain
from .codex_rpc import RpcDeadlineError, RpcOutboundUnavailableError
from .codex_rpc import RpcError as RpcError
from .codex_rpc import RpcRejectedError as RpcRejectedError
from .codex_transports import (
    StdioJsonLineTransport as StdioJsonLineTransport,
)
from .codex_transports import (
    UnixJsonLineTransport as UnixJsonLineTransport,
)
from .codex_transports import (
    UnixWebSocketTransport as UnixWebSocketTransport,
)
from .diagnostic_log import survived

DEFAULT_RPC_RESPONSE_SECONDS = 120.0
DEFAULT_RPC_QUIET_SECONDS = 20.0
TURN_START_RESPONSE_SECONDS = 300.0


def _validate_legacy_permission_profile(result: dict[str, Any]) -> None:
    profile = result.get("activePermissionProfile")
    if profile is None:
        return  # Older servers and explicitly selected legacy policies.
    if (
        isinstance(profile, dict)
        and profile.get("id") == ":workspace"
        and "extends" in profile
        and profile.get("extends") is None
    ):
        return
    # ID/extends and the legacy sandbox projection cannot prove a custom
    # profile is equivalent or stricter. Do not replace it at turn/start.
    raise UnsupportedCodexPermissionProfileError()


class CodexMetadataError(RpcError):
    """Safe capability/precondition failure; never contains provider payloads."""


def validate_codex_thread_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise CodexMetadataError("invalid_thread_id")


def _bounded_partial_text(text: str) -> str:
    return (
        "[Earlier partial text omitted]\n" + text[-(MAX_PARTIAL_TEXT - 40) :]
        if len(text) > MAX_PARTIAL_TEXT
        else text
    )


class CodexTurnError(RpcError):
    """A failed wait retains visible output without claiming task success."""

    def __init__(self, cause: BaseException, partial_text: str = "") -> None:
        super().__init__(str(cause))
        self.partial_text = _bounded_partial_text(partial_text)
        self.failure_reason = codex_failure_reason(cause)


def _final_visible_text(items: Sequence[tuple[str, str]]) -> str:
    """Keep commentary out of a completed answer, including recovered turns."""
    explicit = [text for phase, text in items if phase == "final_answer" and text]
    legacy = [text for phase, text in items if phase == "unknown" and text]
    return "\n\n".join(explicit if explicit else legacy).strip()


class MessageTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, *, timeout: float | None = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CodexThread:
    thread_id: str
    cwd: Path
    model: str
    model_provider: str
    permission_profile: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadMetadata:
    thread_id: str
    cwd: Path
    model_provider: str
    status: str


@dataclass(frozen=True, slots=True)
class ConnectableCodexThread:
    """Bounded discovery metadata; prompt previews and store paths are omitted."""

    thread_id: str
    safe_label: str
    updated_at: int

    def to_public_dict(self) -> dict[str, object]:
        return {"label": self.safe_label, "updated_at": self.updated_at}


@dataclass(frozen=True, slots=True)
class LimitWindow:
    remaining_percent: int
    resets_at: int | None
    duration_minutes: int | None


@dataclass(frozen=True, slots=True)
class RateLimits:
    primary: LimitWindow | None
    secondary: LimitWindow | None


@dataclass(frozen=True, slots=True)
class TurnResult:
    text: str
    context_window: int | None
    context_tokens_used: int | None


@dataclass(frozen=True, slots=True)
class StoredTurnOutcome:
    """Read-only status of one exact persisted turn."""

    status: Literal["completed", "failed", "interrupted", "active", "unknown"]
    result: TurnResult | None = None


def context_remaining_percent(result: TurnResult) -> float | None:
    if (
        result.context_window is None
        or result.context_window <= 0
        or result.context_tokens_used is None
        or result.context_tokens_used < 0
    ):
        return None
    remaining = max(0, result.context_window - result.context_tokens_used)
    return remaining * 100 / result.context_window


class CodexAppServerClient:
    """Small typed client for the stable v2 methods Project Hub needs."""

    def __init__(
        self,
        transport: MessageTransport,
        *,
        initialized: bool = False,
        approval_policy: str = "on-request",
        model_provider: str | None = None,
        permission_profile: str | None = None,
        retire_completed_connection: bool = False,
        transport_mode: Literal["socket", "stdio-fallback"] | None = None,
    ) -> None:
        if approval_policy not in {"on-request", "never"}:
            raise ValueError("unsupported Codex approval policy")
        self._transport = transport
        self._transport_mode: Literal["socket", "stdio-fallback"] | None = transport_mode
        self._response_drain = CodexResponseDrain()
        self._initialized = initialized
        self._approval_policy = approval_policy
        self._model_provider = model_provider
        self._permission_profile = validate_permission_profile_id(permission_profile)
        self._permission_binding: CodexPermissionBinding | None = None
        self._permission_drifted = False
        self._permission_preparing = False
        self._completed_connection = CompletedConnectionProof(enabled=retire_completed_connection)
        self._preparation_thread_id: str | None = None
        self._preparation_settings: list[dict[str, Any]] = []
        self._session_providers = tuple(dict.fromkeys(("openai", model_provider or "openai")))
        self._next_request_id = 1
        self.notifications: deque[dict[str, Any]] = deque()
        # Windows from the current turn's `account/rateLimits/updated` events, by
        # limit. A route whose `account/rateLimits/read` has no windows (a custom
        # model provider) still reports them in these header-derived updates.
        # They are collected only from `turn/start` until the turn ends.
        self._turn_rate_limits: dict[str, RateLimits] = {}
        self._collecting_rate_limits = False
        self.on_visible_item: Callable[[str, str, str], None] | None = None
        self.on_completed: Callable[[TurnResult], None] | None = None
        # Install before start_turn; callbacks begin only at accepted-turn wait.
        self.on_activity: Callable[[CodexActivityEvent], None] | None = None
        self.on_preacceptance_approval: Callable[[CodexActivityEvent], None] | None = None
        self.on_activity_unavailable: Callable[[], None] | None = None
        self._turn_start_pending = False
        self._activity_thread_id: str | None = None
        self._activity_turn_id: str | None = None
        self._activity_ready = False
        self._pending_activity: deque[CodexActivityEvent] = deque()
        self._activity_request_tracker = CodexActivityRequests()
        self._activity_requests = self._activity_request_tracker.pending
        self._activity_retired = False
        self._activity_observed_notifications: set[int] = set()

    @property
    def transport_mode(self) -> Literal["socket", "stdio-fallback"] | None:
        """The acquired connection's mechanism, independent of supervisor selection."""
        return self._transport_mode

    def close(self) -> None:
        self._completed_connection.invalidate()
        self._clear_activity()
        self._transport.close()

    def consume_completed_connection(self, *, thread_id: str, turn_id: str) -> bool:
        """Authorize local retirement once; never unsubscribe or mutate a saved thread."""
        return self._completed_connection.consume(thread_id=thread_id, turn_id=turn_id)

    def _clear_activity(self) -> None:
        self._turn_start_pending = False
        self._activity_thread_id = self._activity_turn_id = None
        self._activity_ready = False
        self._pending_activity.clear()
        self._activity_request_tracker.clear()
        self._activity_retired = False
        self._activity_observed_notifications.clear()

    def _observe_activity(self, message: dict[str, Any]) -> None:
        if self._activity_retired:
            return
        try:
            self._observe_available_activity(message)
        except ActivityObservationUnavailable as error:
            self._activity_retired = True
            self._pending_activity.clear()
            self._activity_request_tracker.clear()
            self.on_activity = self.on_preacceptance_approval = None
            # Accepted IDs also fence mandatory completion/connection proofs.
            # Do not clear them when only passive observation becomes unavailable.
            survived("codex_activity.retired", error)
            if self.on_activity_unavailable is not None:
                try:
                    self.on_activity_unavailable()
                except Exception as cleanup_error:
                    survived("codex_activity.retirement_callback", cleanup_error)

    def _observe_available_activity(self, message: dict[str, Any]) -> None:
        if (
            self.on_activity is None and self.on_preacceptance_approval is None
        ) or self._activity_thread_id is None:
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        event: CodexActivityEvent | None
        if message.get("method") == "serverRequest/resolved":
            event = self._activity_request_tracker.resolve(message)
            if event is None:
                return
        else:
            turn_id = self._activity_turn_id or params.get("turnId")
            if not isinstance(turn_id, str):
                return
            event = normalize_codex_activity(
                message, expected_thread_id=self._activity_thread_id, expected_turn_id=turn_id
            )
            if event is None:
                return
            if event.kind == "approval_requested" and self._approval_policy == "never":
                # The request was already declined locally by _handle_server_request.
                # An unreachable human host must not be advertised as waiting.
                return
            if event.kind == "approval_requested" and event.request_id is not None:
                if not self._activity_request_tracker.request(event):
                    return
        if (
            self._turn_start_pending
            and event.kind in {"approval_requested", "approval_resolved"}
            and self.on_preacceptance_approval is not None
        ):
            self.on_preacceptance_approval(event)
        if not self._activity_ready:
            if len(self._pending_activity) >= MAX_PENDING_ACTIVITY:
                raise ActivityObservationUnavailable("activity_pending_events_exhausted")
            self._pending_activity.append(event)
        elif event.turn_id == self._activity_turn_id and self.on_activity is not None:
            self.on_activity(event)

    def _approval_params(self) -> dict[str, str]:
        params = {"approvalPolicy": self._approval_policy}
        if self._approval_policy == "on-request" or self._permission_profile is not None:
            params["approvalsReviewer"] = "user"
        return params

    @property
    def permission_profile(self) -> str | None:
        return self._permission_profile

    def _prepare_permission_selection(self, cwd: Path) -> dict[str, str]:
        self._permission_binding = None
        self._permission_drifted = False
        self._permission_preparing = False
        self._preparation_settings.clear()
        if self._permission_profile is None:
            return {"sandbox": "workspace-write"}
        verify_managed_selection(self._request, self._permission_profile, cwd)
        self._permission_preparing = True
        return {"permissions": self._permission_profile}

    def _bind_permission_selection(self, result: dict[str, Any], thread_id: str, cwd: Path) -> None:
        if self._permission_profile is None:
            return  # Legacy validation already ran before the sandbox check.
        binding = CodexPermissionBinding(
            thread_id,
            cwd,
            self._permission_profile,
            self._approval_policy,
            self._model_provider or "openai",
        )
        binding.validate(result)
        for params in self._preparation_settings:
            if params.get("threadId") == thread_id:
                binding.validate(params.get("threadSettings"))
        self._permission_binding = binding

    @contextmanager
    def _permission_preparation(
        self, cwd: Path, *, thread_id: str | None = None
    ) -> Iterator[dict[str, str]]:
        try:
            params = self._prepare_permission_selection(cwd)
            self._preparation_thread_id = thread_id
            yield params
        finally:
            self._permission_preparing = False
            self._preparation_thread_id = None
            self._preparation_settings.clear()

    def _observe_permission_settings(self, message: dict[str, Any]) -> None:
        binding = self._permission_binding
        if message.get("method") != "thread/settings/updated" or "id" in message:
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        if binding is None:
            if self._permission_preparing:
                if (
                    self._preparation_thread_id is not None
                    and params.get("threadId") != self._preparation_thread_id
                ):
                    return
                if len(self._preparation_settings) >= 32:
                    raise CodexPermissionProfileError()
                self._preparation_settings.append(params)
            return
        if params.get("threadId") != binding.thread_id:
            return
        try:
            binding.validate(params.get("threadSettings"))
        except CodexPermissionProfileError:
            self._permission_drifted = True

    def _refuse_permission_drift(self, turn_id: str, partial: str) -> None:
        if not self._permission_drifted:
            return
        # Only the worker's durable exact-target fence may authorize interruption.
        # Wake recovery immediately rather than issuing an unfenced client RPC.
        raise CodexTurnError(CodexPermissionPolicyDriftError(), partial)

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
        if self._approval_policy != "never":
            return False
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None or not isinstance(method, str):
            return False
        result: dict[str, Any] | None = None
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": [], "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        if result is None:
            response: dict[str, Any] = {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "server request unavailable in headless stdio fallback",
                },
            }
        else:
            response = {"id": request_id, "result": result}
        try:
            self._transport.send(response)
        except RpcOutboundUnavailableError:
            # Only a known terminal admission refusal is recoverable here.
            # Queue admission never proves that a decline was delivered.
            self._response_drain.record(now=time.monotonic())
        return True

    def _response_remaining(
        self, seconds: float = float("inf"), *, deadline: float | None = None
    ) -> float:
        now = time.monotonic()
        self._response_drain.observe(self._transport, now=now)
        if deadline is not None:
            seconds = min(seconds, deadline - now)
        return self._response_drain.remaining(now=now, seconds=seconds)

    def _receive(self, *, timeout: float) -> dict[str, Any]:
        if isinstance(self._transport, StdioJsonLineTransport):
            return self._transport.receive(
                timeout=timeout, response_remaining=self._response_remaining
            )
        return self._transport.receive(timeout=timeout)

    def _request(
        self, method: str, params: dict[str, Any], *, deadline: float | None = None
    ) -> Any:
        default_deadline = deadline is None
        if default_deadline:
            deadline = time.monotonic() + DEFAULT_RPC_RESPONSE_SECONDS
        assert deadline is not None
        if self._response_remaining(deadline=deadline) <= 0:
            raise RpcDeadlineError()
        request_id = self._next_request_id
        self._next_request_id += 1
        self._transport.send({"method": method, "id": request_id, "params": params})
        while True:
            remaining = self._response_remaining(deadline=deadline)
            if remaining <= 0:
                raise RpcDeadlineError()
            # Foreign frames cannot renew the total response deadline. Keep
            # the existing quiet ceiling only for calls without an explicit
            # deadline; early human approvals use turn/start's longer budget.
            message = self._receive(
                timeout=min(remaining, DEFAULT_RPC_QUIET_SECONDS) if default_deadline else remaining
            )
            if self._response_remaining(deadline=deadline) <= 0:
                raise RpcDeadlineError()
            if "method" in message and "id" in message:
                # A companion client such as tlive owns remote approval. Do
                # not answer from this headless bridge and never auto-allow.
                # If nobody answers, Codex remains blocked (fail-closed).
                self._handle_server_request(message)
                self._observe_activity(message)
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    error = message.get("error") or {}
                    raise RpcRejectedError(str(error.get("message") or "Codex RPC error"))
                if "result" not in message:
                    raise RpcError(f"Codex RPC response for {method} has no result")
                return message["result"]
            # Notifications can arrive while a request is outstanding. Keep
            # only bounded protocol objects; hidden reasoning is never emitted
            # to Telegram by this client.
            if "method" in message and "id" not in message:
                self._observe_permission_settings(message)
                if message.get("method") == "serverRequest/resolved":
                    self._observe_activity(message)
                    continue
                if message.get("method") == "account/rateLimits/updated":
                    update = message.get("params")
                    if isinstance(update, dict):
                        self._observe_rate_limits(update.get("rateLimits"))
                    continue
                if self.on_activity is not None and self._activity_thread_id is not None:
                    self._observe_activity(message)
                if not retain_turn_notification(
                    message,
                    thread_id=self._activity_thread_id,
                    turn_id=self._activity_turn_id,
                ):
                    continue
                if len(self.notifications) >= 1024:
                    raise RpcError("Codex notification buffer exceeded its bound")
                if self.on_activity is not None:
                    # The raw notification still serves visible output and telemetry.
                    # Its activity has already been buffered in receive order.
                    self._activity_observed_notifications.add(id(message))
                self.notifications.append(message)
                continue

    def initialize(self, *, deadline: float | None = None) -> None:
        if self._initialized:
            return
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "hermes-project-hub",
                    "title": "Agents Projects Hub",
                    "version": "0.2.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            deadline=deadline,
        )
        self._transport.send({"method": "initialized", "params": {}})
        self._initialized = True

    def read_thread_metadata(
        self, *, thread_id: str, cwd: Path, deadline: float | None = None
    ) -> CodexThreadMetadata:
        """Inspect exact persisted metadata without loading history or resuming.

        The safe subset is checked against codex-cli 0.154.0's generated v2
        ThreadReadResponse. Runtime idle is not proof that another CLI is closed.
        """
        validate_codex_thread_id(thread_id)
        if not self._initialized:
            raise CodexMetadataError("client_not_initialized")
        result = self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
            deadline=time.monotonic() + 10 if deadline is None else deadline,
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexMetadataError("source_identity_mismatch")
        raw_cwd = thread.get("cwd")
        if not isinstance(raw_cwd, str) or len(raw_cwd) > 4096 or not Path(raw_cwd).is_absolute():
            raise CodexMetadataError("source_root_invalid")
        try:
            source_root = Path(raw_cwd).resolve(strict=True)
            expected_root = cwd.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            raise CodexMetadataError("source_root_invalid") from None
        if source_root != expected_root:
            raise CodexMetadataError("source_root_mismatch")
        if (
            thread.get("ephemeral") is not False
            or thread.get("modelProvider") not in self._session_providers
        ):
            raise CodexMetadataError("source_backend_unsupported")
        if thread.get("source") not in ("cli", "vscode", "exec", "appServer"):
            raise CodexMetadataError("source_kind_unsupported")
        if thread.get("historyMode", "legacy") not in ("legacy", "paginated"):
            raise CodexMetadataError("source_history_unsupported")
        if thread.get("turns", []) != []:
            raise CodexMetadataError("source_metadata_shape_invalid")
        status = thread.get("status")
        if not isinstance(status, dict) or status.get("type") not in ("idle", "notLoaded"):
            raise CodexMetadataError("source_not_idle")
        if status.get("activeFlags", []) != []:
            raise CodexMetadataError("source_not_idle")
        return CodexThreadMetadata(
            thread_id, source_root, str(thread["modelProvider"]), status["type"]
        )

    def list_connectable_threads(
        self,
        *,
        root: Path,
        limit: int = 24,
        deadline: float | None = None,
    ) -> tuple[ConnectableCodexThread, ...]:
        """List a bounded exact-root page without loading transcripts or resuming."""
        if not self._initialized:
            raise CodexMetadataError("client_not_initialized")
        if not 1 <= limit <= 24:
            raise CodexMetadataError("source_list_limit_invalid")
        try:
            canonical_root = root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise CodexMetadataError("source_root_invalid") from None
        result = self._request(
            "thread/list",
            {
                "cwd": str(canonical_root),
                "limit": limit,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "modelProviders": list(self._session_providers),
                "sourceKinds": ["cli", "vscode"],
                "archived": False,
                "useStateDbOnly": True,
            },
            deadline=time.monotonic() + 10 if deadline is None else deadline,
        )
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or len(data) > limit:
            raise CodexMetadataError("source_list_shape_invalid")
        discovered: list[ConnectableCodexThread] = []
        for raw in data:
            if not isinstance(raw, dict):
                raise CodexMetadataError("source_list_shape_invalid")
            thread_id = raw.get("id")
            raw_cwd = raw.get("cwd")
            status = raw.get("status")
            updated_at = raw.get("updatedAt")
            if (
                not isinstance(thread_id, str)
                or not isinstance(raw_cwd, str)
                or not isinstance(status, dict)
                or status.get("type") not in ("idle", "notLoaded")
                or status.get("activeFlags", []) != []
                or raw.get("ephemeral") is not False
                or raw.get("modelProvider") not in self._session_providers
                or raw.get("source") not in ("cli", "vscode")
                or not isinstance(updated_at, int)
                or isinstance(updated_at, bool)
                or updated_at < 0
            ):
                continue
            try:
                if Path(raw_cwd).resolve(strict=True) != canonical_root:
                    continue
                validate_codex_thread_id(thread_id)
            except (OSError, RuntimeError, ValueError, CodexMetadataError):
                continue
            raw_name = raw.get("name")
            name = ""
            if isinstance(raw_name, str):
                compact = " ".join(raw_name.split())
                if (
                    1 <= len(compact) <= 64
                    and "/" not in compact
                    and "\\" not in compact
                    and "<" not in compact
                    and ">" not in compact
                ):
                    name = compact
            timestamp = datetime.fromtimestamp(updated_at, timezone.utc).strftime("%Y-%m-%d %H:%M")
            suffix = "".join(character for character in thread_id if character.isalnum())[-6:]
            label = f"{name or 'Сессия'} · {timestamp} UTC · {suffix or 'saved'}"
            discovered.append(ConnectableCodexThread(thread_id, label[:160], updated_at))
        return tuple(discovered)

    def start_thread(
        self,
        *,
        cwd: Path,
        model: str,
        project_id: str,
        developer_instructions: str | None = None,
    ) -> CodexThread:
        self._completed_connection.invalidate()
        self._clear_activity()
        if not self._initialized:
            raise RpcError("Codex client is not initialized")
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        with self._permission_preparation(canonical_cwd) as permission_params:
            instruction_params = (
                {"developerInstructions": developer_instructions}
                if developer_instructions is not None
                else {}
            )
            result = self._request(
                "thread/start",
                {
                    "cwd": str(canonical_cwd),
                    "model": model,
                    **permission_params,
                    **self._approval_params(),
                    **instruction_params,
                    "experimentalRawEvents": False,
                    **({"modelProvider": self._model_provider} if self._model_provider else {}),
                },
            )
            if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
                raise RpcError("thread/start returned an invalid result")
            if self._model_provider and result.get("modelProvider") != self._model_provider:
                raise RpcError("thread/start returned a different model provider")
            returned_cwd = Path(str(result.get("cwd"))).resolve(strict=True)
            if returned_cwd != canonical_cwd:
                raise RpcError("thread/start returned a different cwd")
            if result.get("approvalPolicy") != self._approval_policy:
                raise RpcError("thread/start returned an unsafe approval policy")
            sandbox = result.get("sandbox")
            if self._permission_profile is None:
                _validate_legacy_permission_profile(result)
            sandbox_is_safe = sandbox == "workspace-write" or (
                isinstance(sandbox, dict) and sandbox.get("type") == "workspaceWrite"
            )
            if not sandbox_is_safe:
                raise RpcError("thread/start returned an unsafe sandbox")
            thread_id = result["thread"].get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise RpcError("thread/start did not return a thread id")
            self._bind_permission_selection(result, thread_id, canonical_cwd)
            return CodexThread(
                thread_id=thread_id,
                cwd=returned_cwd,
                model=str(result.get("model") or model),
                model_provider=str(result.get("modelProvider") or "unknown"),
                permission_profile=self._permission_profile,
            )

    def resume_thread(
        self,
        *,
        thread_id: str,
        cwd: Path,
        model: str,
        developer_instructions: str | None = None,
    ) -> CodexThread:
        self._completed_connection.invalidate()
        self._clear_activity()
        if not self._initialized:
            raise RpcError("Codex client is not initialized")
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        with self._permission_preparation(canonical_cwd, thread_id=thread_id) as permission_params:
            instruction_params = (
                {"developerInstructions": developer_instructions}
                if developer_instructions is not None
                else {}
            )
            result = self._request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "cwd": str(canonical_cwd),
                    "model": model,
                    **permission_params,
                    **self._approval_params(),
                    **instruction_params,
                    "excludeTurns": True,
                    **({"modelProvider": self._model_provider} if self._model_provider else {}),
                },
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            returned_id = thread.get("id") if isinstance(thread, dict) else None
            if self._model_provider and (
                not isinstance(result, dict) or result.get("modelProvider") != self._model_provider
            ):
                raise RpcError("thread/resume returned a different model provider")
            returned_cwd = result.get("cwd") if isinstance(result, dict) else None
            if returned_id != thread_id:
                raise RpcError("thread/resume returned a different thread id")
            if Path(str(returned_cwd)).resolve(strict=True) != canonical_cwd:
                raise RpcError("thread/resume returned a different cwd")
            if result.get("approvalPolicy") != self._approval_policy:
                raise RpcError("thread/resume returned an unsafe approval policy")
            sandbox = result.get("sandbox")
            if self._permission_profile is None:
                _validate_legacy_permission_profile(result)
            if not (
                sandbox == "workspace-write"
                or (isinstance(sandbox, dict) and sandbox.get("type") == "workspaceWrite")
            ):
                raise RpcError("thread/resume returned an unsafe sandbox")
            self._bind_permission_selection(result, thread_id, canonical_cwd)
            return CodexThread(
                thread_id=thread_id,
                cwd=canonical_cwd,
                model=str(result.get("model") or model),
                model_provider=str(result.get("modelProvider") or "unknown"),
                permission_profile=self._permission_profile,
            )

    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        text: str,
        model: str,
        effort: str,
        local_image_paths: Sequence[Path] = (),
    ) -> str:
        self._completed_connection.invalidate()
        self._clear_activity()
        self._response_drain = CodexResponseDrain()
        self.notifications.clear()
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        if self._permission_profile is not None and (
            self._permission_binding is None
            or self._permission_binding.thread_id != thread_id
            or self._permission_binding.root != canonical_cwd
            or self._permission_drifted
        ):
            raise CodexPermissionProfileError()
        permission_params: dict[str, Any] = (
            {"permissions": self._permission_profile}
            if self._permission_profile is not None
            else {
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(canonical_cwd)],
                    "networkAccess": False,
                }
            }
        )
        turn_input: list[dict[str, str]] = [{"type": "text", "text": text}]
        for image_path in local_image_paths:
            if image_path.is_symlink():
                raise RpcError("turn/start local image must not be a symlink")
            canonical_image = image_path.expanduser().resolve(strict=True)
            if not canonical_image.is_file() or not canonical_image.is_relative_to(canonical_cwd):
                raise RpcError("turn/start local image is outside the execution root")
            turn_input.append({"type": "localImage", "path": str(canonical_image)})
        # Updates arriving during turn/start belong to this turn; older ones do not.
        self._turn_rate_limits = {}
        self._collecting_rate_limits = True
        self._activity_thread_id = thread_id
        self._turn_start_pending = True
        try:
            result = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": str(canonical_cwd),
                    "input": turn_input,
                    "model": model,
                    "effort": effort,
                    **self._approval_params(),
                    **permission_params,
                },
                deadline=time.monotonic() + TURN_START_RESPONSE_SECONDS,
            )
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise RpcError("turn/start did not return a turn id")
        except BaseException:
            # Acceptance is unconfirmed: the submission may have started a
            # turn. Clear unattributed telemetry; do not infer replay safety.
            self._collecting_rate_limits = False
            self._clear_activity()
            raise
        finally:
            self._turn_start_pending = False
        self._activity_turn_id = turn_id
        return turn_id

    def interrupt_turn(
        self, *, thread_id: str, turn_id: str, deadline: float | None = None
    ) -> None:
        result = self._request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            deadline=min(deadline, time.monotonic() + 10)
            if deadline is not None
            else time.monotonic() + 10,
        )
        if result is not None and not isinstance(result, dict):
            raise RpcError("turn/interrupt returned an invalid result")

    def steer_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        text: str,
        client_user_message_id: str,
    ) -> str:
        if self._permission_profile is not None:
            raise CodexPermissionProfileError()
        result = self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "clientUserMessageId": client_user_message_id,
                "input": [{"type": "text", "text": text}],
            },
            deadline=time.monotonic() + 10,
        )
        returned_turn = result.get("turnId") if isinstance(result, dict) else None
        if not isinstance(returned_turn, str) or not returned_turn:
            raise RpcError("turn/steer did not return a turn id")
        return returned_turn

    def wait_for_turn(self, turn_id: str) -> TurnResult:
        """Wait for one turn while excluding hidden reasoning from the result."""
        self._completed_connection.invalidate()
        try:
            return self._wait_for_turn(turn_id)
        except CodexTurnError as exc:
            # Keep permission/storage/provider failures authoritative. The
            # channel warning is visible context, not a new failure cause.
            self._response_drain.observe(self._transport, now=time.monotonic())
            exc.partial_text = _bounded_partial_text(
                self._response_drain.annotate(exc.partial_text)
            )
            raise
        except Exception as exc:
            self._response_drain.observe(self._transport, now=time.monotonic())
            raise CodexTurnError(exc, self._response_drain.annotate("")) from exc
        finally:
            self._collecting_rate_limits = False
            self._clear_activity()

    def _wait_for_turn(self, turn_id: str) -> TurnResult:
        answers: list[str] = []
        final_items: list[tuple[str, str]] = []
        seen_items: set[str] = set()
        context_window: int | None = None
        context_tokens_used: int | None = None
        self._refuse_permission_drift(turn_id, "")
        # The worker enters this method only after persisting native acceptance.
        # An early request supplies IDs to validate, never authority to bind a job.
        self._activity_ready = self._activity_turn_id == turn_id
        try:
            while self._pending_activity:
                event = self._pending_activity.popleft()
                if (
                    self._activity_ready
                    and event.turn_id == turn_id
                    and self.on_activity is not None
                ):
                    self.on_activity(event)
        except Exception as exc:
            raise CodexTurnError(exc, "") from exc
        while True:
            # Model turns routinely exceed the short RPC handshake timeout.
            # Keep a finite ceiling so a lost app-server cannot strand a worker
            # forever; the worker heartbeat protects the durable job meanwhile.
            try:
                remaining = self._response_remaining(3600.0)
                message = (
                    self.notifications.popleft()
                    if self.notifications
                    else self._receive(timeout=remaining)
                )
                self._response_remaining(3600.0)
            except Exception as exc:
                raise CodexTurnError(exc, "\n\n".join(answers)) from exc
            method = message.get("method")
            self._observe_permission_settings(message)
            self._refuse_permission_drift(turn_id, "\n\n".join(answers))
            if method and "id" in message:
                # tlive answers approvals on its companion connection.
                # This client deliberately neither allows nor denies.
                try:
                    self._handle_server_request(message)
                    self._response_remaining(3600.0)
                    self._observe_activity(message)
                except Exception as exc:
                    raise CodexTurnError(exc, "\n\n".join(answers)) from exc
                continue
            try:
                if id(message) in self._activity_observed_notifications:
                    self._activity_observed_notifications.remove(id(message))
                else:
                    self._observe_activity(message)
            except Exception as exc:
                raise CodexTurnError(exc, "\n\n".join(answers)) from exc
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            if method == "account/rateLimits/updated":
                self._observe_rate_limits(params.get("rateLimits"))
                continue
            if (
                self._activity_thread_id is not None
                and "threadId" in params
                and params["threadId"] != self._activity_thread_id
            ):
                continue
            if method == "thread/tokenUsage/updated" and params.get("turnId") == turn_id:
                usage = params.get("tokenUsage")
                if isinstance(usage, dict):
                    window = usage.get("modelContextWindow")
                    last = usage.get("last")
                    if isinstance(window, int):
                        context_window = window
                    if isinstance(last, dict) and isinstance(last.get("totalTokens"), int):
                        context_tokens_used = last["totalTokens"]
                continue
            if method == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item")
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and isinstance(item.get("text"), str)
                ):
                    item_id = item.get("id")
                    if not isinstance(item_id, str) or item_id not in seen_items:
                        answers.append(item["text"])
                        final_items.append((str(item.get("phase") or "unknown"), item["text"]))
                        if isinstance(item_id, str):
                            seen_items.add(item_id)
                            if self.on_visible_item is not None and item["text"].strip():
                                phase = item.get("phase") or "unknown"
                                try:
                                    self.on_visible_item(item_id, item["text"], str(phase))
                                except Exception as exc:
                                    raise CodexTurnError(exc, "\n\n".join(answers)) from exc
                continue
            if method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, dict) and turn.get("id") == turn_id:
                    if turn.get("status") in {"failed", "interrupted"}:
                        error = turn.get("error")
                        message = error.get("message") if isinstance(error, dict) else None
                        raise CodexTurnError(
                            RpcError(str(message or f"Codex turn {turn.get('status')}")),
                            "\n\n".join(answers),
                        )
                    try:
                        self._response_remaining(3600.0)
                    except Exception as exc:
                        raise CodexTurnError(exc, "\n\n".join(answers)) from exc
                    if not self._response_drain.proves_completed(
                        params,
                        thread_id=self._activity_thread_id,
                        turn_id=turn_id,
                        accepted_turn_id=self._activity_turn_id,
                    ):
                        raise CodexTurnError(
                            RpcError(
                                "Codex completion proof unavailable after response channel failure"
                            ),
                            "\n\n".join(answers),
                        )
                    result = TurnResult(
                        text=self._response_drain.annotate(_final_visible_text(final_items)),
                        context_window=context_window,
                        context_tokens_used=context_tokens_used,
                    )
                    if self.on_completed is not None:
                        try:
                            self.on_completed(result)
                        except Exception as exc:
                            raise CodexTurnError(exc, "\n\n".join(answers)) from exc
                    self._completed_connection.observe(
                        thread_id=self._activity_thread_id,
                        turn_id=self._activity_turn_id,
                        params=params,
                    )
                    return result
            if method == "error" and params.get("turnId") == turn_id:
                # Current app-server nests the public message under ``error``.
                # A retrying notification is informational; the terminal event
                # arrives later and must remain the source of truth.
                if params.get("willRetry") is True:
                    continue
                error = params.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                raise CodexTurnError(
                    RpcError(str(message or params.get("message") or "Codex turn failed")),
                    "\n\n".join(answers),
                )

    def read_turn_outcome(
        self, *, thread_id: str, turn_id: str, cwd: Path, deadline: float | None = None
    ) -> StoredTurnOutcome:
        """Read one exact persisted turn without resume, subscribe, or inference."""
        summary = self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
            deadline=min(deadline, time.monotonic() + 10)
            if deadline is not None
            else time.monotonic() + 10,
        )
        thread = summary.get("thread") if isinstance(summary, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise RpcError("stored thread identity mismatch")
        stored_cwd = thread.get("cwd")
        if not isinstance(stored_cwd, str) or Path(stored_cwd).resolve() != cwd.resolve(
            strict=True
        ):
            raise RpcError("stored thread project root mismatch")
        history_mode = thread.get("historyMode", "legacy")
        if history_mode == "paginated":
            return self._read_paginated_turn_outcome(
                thread_id=thread_id, turn_id=turn_id, deadline=deadline
            )
        if history_mode != "legacy":
            raise RpcError("stored thread history mode is unsupported")
        result = self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}, deadline=deadline
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise RpcError("stored thread identity mismatch")
        stored_cwd = thread.get("cwd")
        if not isinstance(stored_cwd, str) or Path(stored_cwd).resolve() != cwd.resolve(
            strict=True
        ):
            raise RpcError("stored thread project root mismatch")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise RpcError("stored thread has no turn history")
        matches = [turn for turn in turns if isinstance(turn, dict) and turn.get("id") == turn_id]
        if not matches:
            return StoredTurnOutcome("unknown")
        if len(matches) != 1:
            raise RpcError("stored turn identity is ambiguous")
        status = matches[0].get("status")
        if status in {"failed", "interrupted"}:
            return StoredTurnOutcome(cast(Literal["failed", "interrupted"], status))
        if status in {"inProgress", "in_progress"}:
            return StoredTurnOutcome("active")
        if status != "completed":
            raise RpcError("stored turn status is unrecognized")
        items = matches[0].get("items")
        if not isinstance(items, list):
            raise RpcError("completed turn has no visible item history")
        answers: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            text, item_id = item.get("text"), item.get("id")
            if not isinstance(text, str) or not isinstance(item_id, str) or item_id in seen:
                continue
            seen.add(item_id)
            answers.append((str(item.get("phase") or "unknown"), text))
        text = _final_visible_text(answers)
        if len(text) > 200_000:
            raise RpcError("stored visible response exceeds recovery bound")
        return StoredTurnOutcome("completed", TurnResult(text, None, None))

    def _read_paginated_turn_outcome(
        self, *, thread_id: str, turn_id: str, deadline: float | None = None
    ) -> StoredTurnOutcome:
        cursor: str | None = None
        used_cursors: set[str] = set()
        search_deadline = (
            min(deadline, time.monotonic() + 15) if deadline is not None else time.monotonic() + 15
        )
        for _ in range(20):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": 20,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = self._request("thread/turns/list", params, deadline=search_deadline)
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, list) or len(data) > 20:
                raise RpcError("stored turn page has an invalid shape")
            matches = [
                item for item in data if isinstance(item, dict) and item.get("id") == turn_id
            ]
            if len(matches) > 1:
                raise RpcError("stored turn identity is ambiguous")
            if matches:
                status = matches[0].get("status")
                if status in {"failed", "interrupted"}:
                    return StoredTurnOutcome(cast(Literal["failed", "interrupted"], status))
                if status in {"inProgress", "in_progress"}:
                    return StoredTurnOutcome("active")
                if status != "completed":
                    raise RpcError("stored turn status is unrecognized")
                return StoredTurnOutcome(
                    "completed",
                    TurnResult(
                        self._read_paginated_visible_items(thread_id, turn_id, deadline=deadline),
                        None,
                        None,
                    ),
                )
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                return StoredTurnOutcome("unknown")
            if (
                not isinstance(next_cursor, str)
                or not 1 <= len(next_cursor) <= 2048
                or next_cursor in used_cursors
                or not data
            ):
                raise RpcError("stored turn pagination is invalid")
            used_cursors.add(next_cursor)
            cursor = next_cursor
        raise RpcError("stored turn search exceeds recovery bound")

    def _read_paginated_visible_items(
        self, thread_id: str, turn_id: str, *, deadline: float | None = None
    ) -> str:
        cursor: str | None = None
        used_cursors: set[str] = set()
        seen_items: set[str] = set()
        answers: list[tuple[str, str]] = []
        text_size = 0
        deadline = (
            min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30
        )
        for _ in range(100):
            params: dict[str, Any] = {"threadId": thread_id, "turnId": turn_id, "limit": 10}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._request("thread/items/list", params, deadline=deadline)
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, list) or len(data) > 10:
                raise RpcError("stored item page has an invalid shape")
            for row in data:
                if not isinstance(row, dict) or row.get("turnId") != turn_id:
                    raise RpcError("stored item turn identity mismatch")
                item = row.get("item")
                if not isinstance(item, dict) or item.get("type") != "agentMessage":
                    continue
                item_id, visible = item.get("id"), item.get("text")
                if not isinstance(item_id, str) or not isinstance(visible, str):
                    raise RpcError("stored visible item has an invalid shape")
                if item_id in seen_items:
                    continue
                seen_items.add(item_id)
                text_size += len(visible) + 2
                if text_size > 200_000:
                    raise RpcError("stored visible response exceeds recovery bound")
                answers.append((str(item.get("phase") or "unknown"), visible))
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                return _final_visible_text(answers)
            if (
                not isinstance(next_cursor, str)
                or not 1 <= len(next_cursor) <= 2048
                or next_cursor in used_cursors
                or not data
            ):
                raise RpcError("stored item pagination is invalid")
            used_cursors.add(next_cursor)
            cursor = next_cursor
        raise RpcError("stored item history exceeds recovery bound")

    def read_completed_turn(self, *, thread_id: str, turn_id: str, cwd: Path) -> TurnResult | None:
        """Compatibility wrapper for callers that need only completed output."""
        outcome = self.read_turn_outcome(thread_id=thread_id, turn_id=turn_id, cwd=cwd)
        return outcome.result if outcome.status == "completed" else None

    def list_models(self) -> tuple[dict[str, Any], ...]:
        result = self._request("model/list", {"includeHidden": False})
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise RpcError("model/list returned invalid data")
        return tuple(item for item in data if isinstance(item, dict))

    @staticmethod
    def _limit_window(value: object) -> LimitWindow | None:
        if not isinstance(value, dict) or not isinstance(value.get("usedPercent"), int):
            return None
        used = max(0, min(100, value["usedPercent"]))
        resets_at = value.get("resetsAt")
        duration = value.get("windowDurationMins")
        return LimitWindow(
            remaining_percent=100 - used,
            resets_at=resets_at if isinstance(resets_at, int) else None,
            duration_minutes=duration if isinstance(duration, int) else None,
        )

    @staticmethod
    def _limit_id(snapshot: dict[str, Any]) -> str:
        # The read's single-bucket `rateLimits` mirrors the historical `codex`
        # limit; a snapshot without an id is that limit.
        value = snapshot.get("limitId")
        return value if isinstance(value, str) and value else "codex"

    def _observe_rate_limits(self, snapshot: object) -> None:
        """Merge one sparse rolling update into its own limit's windows."""
        if not self._collecting_rate_limits or not isinstance(snapshot, dict):
            return
        limit_id = self._limit_id(snapshot)
        previous = self._turn_rate_limits.get(limit_id) or RateLimits(None, None)
        self._turn_rate_limits[limit_id] = RateLimits(
            primary=self._limit_window(snapshot.get("primary")) or previous.primary,
            secondary=self._limit_window(snapshot.get("secondary")) or previous.secondary,
        )

    def read_rate_limits(self, *, deadline: float | None = None) -> RateLimits:
        """Read the account snapshot, filling missing windows from the last turn.

        The turn's windows are used once, by the read that follows the turn, so a
        later read never presents them as current.
        """
        observed, self._turn_rate_limits = self._turn_rate_limits, {}
        try:
            result = self._request("account/rateLimits/read", {}, deadline=deadline)
        except (RpcError, TimeoutError):
            if "codex" not in observed:
                raise
            return observed["codex"]
        snapshot = result.get("rateLimits") if isinstance(result, dict) else None
        if not isinstance(snapshot, dict):
            if "codex" not in observed:
                raise RpcError("account/rateLimits/read returned invalid data")
            return observed["codex"]
        # Only windows of the same limit may complete the read.
        rolling = observed.get(self._limit_id(snapshot))
        read = RateLimits(
            primary=self._limit_window(snapshot.get("primary")),
            secondary=self._limit_window(snapshot.get("secondary")),
        )
        if rolling is None:
            return read
        return RateLimits(
            primary=read.primary or rolling.primary,
            secondary=read.secondary or rolling.secondary,
        )
