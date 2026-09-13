from __future__ import annotations

import asyncio
import json
import queue
import re
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import aiohttp

from .codex_failure import MAX_PARTIAL_TEXT, codex_failure_reason


class RpcError(RuntimeError):
    pass


class RpcRejectedError(RpcError):
    """The app-server returned an explicit JSON-RPC rejection."""


class CodexMetadataError(RpcError):
    """Safe capability/precondition failure; never contains provider payloads."""


def validate_codex_thread_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise CodexMetadataError("invalid_thread_id")


class CodexTurnError(RpcError):
    """A failed wait retains visible output without claiming task success."""

    def __init__(self, cause: BaseException, partial_text: str = "") -> None:
        super().__init__(str(cause))
        self.partial_text = (
            "[Earlier partial text omitted]\n" + partial_text[-(MAX_PARTIAL_TEXT - 40) :]
            if len(partial_text) > MAX_PARTIAL_TEXT
            else partial_text
        )
        self.failure_reason = codex_failure_reason(cause)


class MessageTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, *, timeout: float | None = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


class UnixJsonLineTransport:
    """Newline-delimited JSON transport for a local Codex app-server socket."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._reader = connection.makefile("r", encoding="utf-8", newline="\n")
        self._writer = connection.makefile("w", encoding="utf-8", newline="\n")

    @classmethod
    def connect(cls, socket_path: Path, *, timeout: float = 20.0) -> "UnixJsonLineTransport":
        path = socket_path.expanduser().resolve(strict=True)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(str(path))
        return cls(connection)

    def send(self, message: dict[str, Any]) -> None:
        self._writer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        self._writer.write("\n")
        self._writer.flush()

    def receive(self, *, timeout: float | None = None) -> dict[str, Any]:
        del timeout
        line = self._reader.readline()
        if not line:
            raise EOFError("Codex app-server closed the connection")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RpcError("Codex app-server emitted malformed JSON") from exc
        if not isinstance(message, dict):
            raise RpcError("Codex app-server message must be an object")
        return message

    def close(self) -> None:
        self._reader.close()
        self._writer.close()
        self._connection.close()


class StdioJsonLineTransport:
    """JSONL transport backed by the official `codex app-server --stdio`."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        if process.stdin is None or process.stdout is None:
            raise RpcError("Codex stdio pipes are unavailable")
        self._process = process
        self._reader = process.stdout
        self._writer = process.stdin
        self._closed = False
        self._lines: queue.Queue[str | BaseException] = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()

    def _read_stdout(self) -> None:
        try:
            while line := self._reader.readline():
                self._lines.put(line)
            self._lines.put(EOFError("Codex app-server closed stdout"))
        except Exception as exc:
            self._lines.put(exc)

    @classmethod
    def start(cls, executable: str = "codex") -> "StdioJsonLineTransport":
        process = subprocess.Popen(
            (executable, "app-server", "--stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        return cls(process)

    def send(self, message: dict[str, Any]) -> None:
        self._writer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        self._writer.write("\n")
        self._writer.flush()

    def receive(self, *, timeout: float | None = None) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=20.0 if timeout is None else timeout)
        except queue.Empty as exc:
            raise RpcError("timed out waiting for Codex stdio") from exc
        if isinstance(line, BaseException):
            raise line
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RpcError("Codex app-server emitted malformed JSON") from exc
        if not isinstance(message, dict):
            raise RpcError("Codex app-server message must be an object")
        return message

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        finally:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
            self._reader_thread.join(timeout=2)
            if not self._reader_thread.is_alive():
                self._reader.close()


class UnixWebSocketTransport:
    """Synchronous facade over Codex's WebSocket-over-Unix transport."""

    def __init__(self, socket_path: Path, *, timeout: float = 20.0) -> None:
        self._socket_path = socket_path.expanduser().resolve(strict=True)
        self._timeout = timeout
        self._outbound: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._inbound: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._ready = threading.Event()
        self._outbound_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.close()
            raise RpcError("timed out connecting to Codex Unix WebSocket")
        if not self._inbound.empty():
            first = self._inbound.queue[0]
            if isinstance(first, BaseException):
                self.close()
                raise RpcError(f"Codex Unix WebSocket failed: {type(first).__name__}")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_owned())
        except BaseException as exc:
            self._inbound.put(exc)
            self._ready.set()

    async def _run_owned(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            if not self._closed:
                await self._run()
        finally:
            self._task = None
            self._loop = None

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        outbound_event = asyncio.Event()
        self._outbound_event = outbound_event
        connector = aiohttp.UnixConnector(path=str(self._socket_path))
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect("http://localhost/") as websocket:
                    self._ready.set()

                    async def sender() -> None:
                        while True:
                            outbound_event.clear()
                            try:
                                message = self._outbound.get_nowait()
                            except queue.Empty:
                                await outbound_event.wait()
                                continue
                            if message is None:
                                await websocket.close()
                                return
                            await websocket.send_json(message)

                    async def receiver() -> None:
                        try:
                            async for message in websocket:
                                if message.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        value = json.loads(message.data)
                                    except json.JSONDecodeError as exc:
                                        self._inbound.put(exc)
                                        continue
                                    if isinstance(value, dict):
                                        self._inbound.put(value)
                                elif message.type == aiohttp.WSMsgType.ERROR:
                                    self._inbound.put(
                                        websocket.exception() or RpcError("Codex WebSocket failed")
                                    )
                                    return
                        finally:
                            self._inbound.put(EOFError("Codex WebSocket closed"))
                            self._outbound.put(None)
                            outbound_event.set()

                    await asyncio.gather(sender(), receiver())
        finally:
            self._outbound_event = None
            self._loop = None

    def _wake_sender(self) -> None:
        loop = self._loop
        event = self._outbound_event
        if loop is not None and event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # Concurrent transport teardown already closed the loop.

    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise RpcError("Codex Unix WebSocket is closed")
        self._outbound.put(message)
        self._wake_sender()

    def receive(self, *, timeout: float | None = None) -> dict[str, Any]:
        try:
            value = self._inbound.get(timeout=self._timeout if timeout is None else timeout)
        except queue.Empty as exc:
            raise RpcError("timed out waiting for Codex Unix WebSocket") from exc
        if isinstance(value, BaseException):
            raise RpcError(f"Codex Unix WebSocket failed: {type(value).__name__}") from value
        return value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._outbound.put(None)
        self._wake_sender()
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The loop completed between inspection and notification.
        self._thread.join(timeout=5)


@dataclass(frozen=True, slots=True)
class CodexThread:
    thread_id: str
    cwd: Path
    model: str
    model_provider: str


@dataclass(frozen=True, slots=True)
class CodexThreadMetadata:
    thread_id: str
    cwd: Path
    model_provider: str
    status: str


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


class CodexAppServerClient:
    """Small typed client for the stable v2 methods Project Hub needs."""

    def __init__(
        self,
        transport: MessageTransport,
        *,
        initialized: bool = False,
        approval_policy: str = "on-request",
    ) -> None:
        if approval_policy not in {"on-request", "never"}:
            raise ValueError("unsupported Codex approval policy")
        self._transport = transport
        self._initialized = initialized
        self._approval_policy = approval_policy
        self._next_request_id = 1
        self.notifications: deque[dict[str, Any]] = deque()
        self.on_visible_item: Callable[[str, str, str], None] | None = None
        self.on_completed: Callable[[TurnResult], None] | None = None

    def close(self) -> None:
        self._transport.close()

    def _approval_params(self) -> dict[str, str]:
        params = {"approvalPolicy": self._approval_policy}
        if self._approval_policy == "on-request":
            params["approvalsReviewer"] = "user"
        return params

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
        if self._approval_policy != "never":
            return False
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None or not isinstance(method, str):
            return False
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result: dict[str, Any] = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": [], "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        else:
            self._transport.send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "server request unavailable in headless stdio fallback",
                    },
                }
            )
            return True
        self._transport.send({"id": request_id, "result": result})
        return True

    def _request(
        self, method: str, params: dict[str, Any], *, deadline: float | None = None
    ) -> Any:
        if deadline is not None and time.monotonic() >= deadline:
            raise RpcError("Codex request deadline exceeded")
        request_id = self._next_request_id
        self._next_request_id += 1
        self._transport.send({"method": method, "id": request_id, "params": params})
        while True:
            if deadline is None:
                message = self._transport.receive()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RpcError("Codex request deadline exceeded")
                message = self._transport.receive(timeout=remaining)
            if "method" in message and "id" in message:
                # A companion client such as tlive owns remote approval. Do
                # not answer from this headless bridge and never auto-allow.
                # If nobody answers, Codex remains blocked (fail-closed).
                self._handle_server_request(message)
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
                if len(self.notifications) >= 1024:
                    raise RpcError("Codex notification buffer exceeded its bound")
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
        if thread.get("ephemeral") is not False or thread.get("modelProvider") != "openai":
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
        return CodexThreadMetadata(thread_id, source_root, "openai", status["type"])

    def start_thread(
        self,
        *,
        cwd: Path,
        model: str,
        project_id: str,
        developer_instructions: str | None = None,
    ) -> CodexThread:
        if not self._initialized:
            raise RpcError("Codex client is not initialized")
        canonical_cwd = cwd.expanduser().resolve(strict=True)
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
                "sandbox": "workspace-write",
                **self._approval_params(),
                **instruction_params,
                "experimentalRawEvents": False,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise RpcError("thread/start returned an invalid result")
        returned_cwd = Path(str(result.get("cwd"))).resolve(strict=True)
        if returned_cwd != canonical_cwd:
            raise RpcError("thread/start returned a different cwd")
        if result.get("approvalPolicy") != self._approval_policy:
            raise RpcError("thread/start returned an unsafe approval policy")
        sandbox = result.get("sandbox")
        sandbox_is_safe = sandbox == "workspace-write" or (
            isinstance(sandbox, dict) and sandbox.get("type") == "workspaceWrite"
        )
        if not sandbox_is_safe:
            raise RpcError("thread/start returned an unsafe sandbox")
        thread_id = result["thread"].get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RpcError("thread/start did not return a thread id")
        return CodexThread(
            thread_id=thread_id,
            cwd=returned_cwd,
            model=str(result.get("model") or model),
            model_provider=str(result.get("modelProvider") or "unknown"),
        )

    def resume_thread(
        self,
        *,
        thread_id: str,
        cwd: Path,
        model: str,
        developer_instructions: str | None = None,
    ) -> CodexThread:
        if not self._initialized:
            raise RpcError("Codex client is not initialized")
        canonical_cwd = cwd.expanduser().resolve(strict=True)
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
                "sandbox": "workspace-write",
                **self._approval_params(),
                **instruction_params,
                "excludeTurns": True,
            },
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        returned_id = thread.get("id") if isinstance(thread, dict) else None
        returned_cwd = result.get("cwd") if isinstance(result, dict) else None
        if returned_id != thread_id:
            raise RpcError("thread/resume returned a different thread id")
        if Path(str(returned_cwd)).resolve(strict=True) != canonical_cwd:
            raise RpcError("thread/resume returned a different cwd")
        if result.get("approvalPolicy") != self._approval_policy:
            raise RpcError("thread/resume returned an unsafe approval policy")
        sandbox = result.get("sandbox")
        if not (
            sandbox == "workspace-write"
            or (isinstance(sandbox, dict) and sandbox.get("type") == "workspaceWrite")
        ):
            raise RpcError("thread/resume returned an unsafe sandbox")
        return CodexThread(
            thread_id=thread_id,
            cwd=canonical_cwd,
            model=str(result.get("model") or model),
            model_provider=str(result.get("modelProvider") or "unknown"),
        )

    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        text: str,
        model: str,
        effort: str,
    ) -> str:
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        result = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "cwd": str(canonical_cwd),
                "input": [{"type": "text", "text": text}],
                "model": model,
                "effort": effort,
                **self._approval_params(),
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(canonical_cwd)],
                    "networkAccess": False,
                },
            },
        )
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise RpcError("turn/start did not return a turn id")
        return turn_id

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        result = self._request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
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
        result = self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "clientUserMessageId": client_user_message_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        returned_turn = result.get("turnId") if isinstance(result, dict) else None
        if not isinstance(returned_turn, str) or not returned_turn:
            raise RpcError("turn/steer did not return a turn id")
        return returned_turn

    def wait_for_turn(self, turn_id: str) -> TurnResult:
        """Wait for one turn while excluding hidden reasoning from the result."""
        answers: list[str] = []
        seen_items: set[str] = set()
        context_window: int | None = None
        context_tokens_used: int | None = None
        while True:
            # Model turns routinely exceed the short RPC handshake timeout.
            # Keep a finite ceiling so a lost app-server cannot strand a worker
            # forever; the worker heartbeat protects the durable job meanwhile.
            try:
                message = (
                    self.notifications.popleft()
                    if self.notifications
                    else self._transport.receive(timeout=3600.0)
                )
            except Exception as exc:
                raise CodexTurnError(exc, "\n\n".join(answers)) from exc
            method = message.get("method")
            if method and "id" in message:
                # tlive answers approvals on its companion connection.
                # This client deliberately neither allows nor denies.
                self._handle_server_request(message)
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            if method == "thread/tokenUsage/updated" and params.get("turnId") == turn_id:
                usage = params.get("tokenUsage")
                if isinstance(usage, dict):
                    window = usage.get("modelContextWindow")
                    total = usage.get("total")
                    if isinstance(window, int):
                        context_window = window
                    if isinstance(total, dict) and isinstance(total.get("totalTokens"), int):
                        context_tokens_used = total["totalTokens"]
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
                        if isinstance(item_id, str):
                            seen_items.add(item_id)
                            if self.on_visible_item is not None and item["text"].strip():
                                phase = item.get("phase") or "unknown"
                                self.on_visible_item(item_id, item["text"], str(phase))
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
                    result = TurnResult(
                        text="\n\n".join(answer for answer in answers if answer).strip(),
                        context_window=context_window,
                        context_tokens_used=context_tokens_used,
                    )
                    if self.on_completed is not None:
                        self.on_completed(result)
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

    def read_completed_turn(self, *, thread_id: str, turn_id: str, cwd: Path) -> TurnResult | None:
        """Read persisted outcome only; never resume, subscribe, or invoke a model."""
        result = self._request("thread/read", {"threadId": thread_id, "includeTurns": True})
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
        if len(matches) != 1 or matches[0].get("status") != "completed":
            return None
        items = matches[0].get("items")
        if not isinstance(items, list):
            return None
        answers: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            text, item_id = item.get("text"), item.get("id")
            if not isinstance(text, str) or not isinstance(item_id, str) or item_id in seen:
                continue
            seen.add(item_id)
            answers.append(text)
        text = "\n\n".join(answers).strip()
        if len(text) > 200_000:
            raise RpcError("stored visible response exceeds recovery bound")
        return TurnResult(text, None, None)

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

    def read_rate_limits(self) -> RateLimits:
        result = self._request("account/rateLimits/read", {})
        snapshot = result.get("rateLimits") if isinstance(result, dict) else None
        if not isinstance(snapshot, dict):
            raise RpcError("account/rateLimits/read returned invalid data")
        return RateLimits(
            primary=self._limit_window(snapshot.get("primary")),
            secondary=self._limit_window(snapshot.get("secondary")),
        )
