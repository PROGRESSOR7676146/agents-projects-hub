from __future__ import annotations

import asyncio
import json
import queue
import socket
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import aiohttp


class RpcError(RuntimeError):
    pass


class RpcRejectedError(RpcError):
    """The app-server returned an explicit JSON-RPC rejection."""


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
        del timeout
        line = self._reader.readline()
        if not line:
            raise EOFError("Codex app-server closed stdout")
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


class UnixWebSocketTransport:
    """Synchronous facade over Codex's WebSocket-over-Unix transport."""

    def __init__(self, socket_path: Path, *, timeout: float = 20.0) -> None:
        self._socket_path = socket_path.expanduser().resolve(strict=True)
        self._timeout = timeout
        self._outbound: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._inbound: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RpcError("timed out connecting to Codex Unix WebSocket")
        if not self._inbound.empty():
            first = self._inbound.queue[0]
            if isinstance(first, BaseException):
                raise RpcError(f"Codex Unix WebSocket failed: {type(first).__name__}")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._inbound.put(exc)
            self._ready.set()

    async def _run(self) -> None:
        connector = aiohttp.UnixConnector(path=str(self._socket_path))
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect("http://localhost/") as websocket:
                self._ready.set()

                async def sender() -> None:
                    while True:
                        message = await asyncio.to_thread(self._outbound.get)
                        if message is None:
                            await websocket.close()
                            return
                        await websocket.send_json(message)

                async def receiver() -> None:
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

                await asyncio.gather(sender(), receiver())

    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise RpcError("Codex Unix WebSocket is closed")
        self._outbound.put(message)

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
        self._thread.join(timeout=5)


@dataclass(frozen=True, slots=True)
class CodexThread:
    thread_id: str
    cwd: Path
    model: str
    model_provider: str


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
        self.notifications: list[dict[str, Any]] = []

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

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._transport.send({"method": method, "id": request_id, "params": params})
        while True:
            message = self._transport.receive()
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
                self.notifications.append(message)
                continue

    def initialize(self) -> None:
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
        )
        self._transport.send({"method": "initialized", "params": {}})
        self._initialized = True

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
        context_window: int | None = None
        context_tokens_used: int | None = None
        while True:
            # Model turns routinely exceed the short RPC handshake timeout.
            # Keep a finite ceiling so a lost app-server cannot strand a worker
            # forever; the worker heartbeat protects the durable job meanwhile.
            message = self._transport.receive(timeout=3600.0)
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
                    answers.append(item["text"])
                continue
            if method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, dict) and turn.get("id") == turn_id:
                    if turn.get("status") in {"failed", "interrupted"}:
                        error = turn.get("error")
                        message = error.get("message") if isinstance(error, dict) else None
                        raise RpcError(str(message or f"Codex turn {turn.get('status')}"))
                    return TurnResult(
                        text="\n\n".join(answer for answer in answers if answer).strip(),
                        context_window=context_window,
                        context_tokens_used=context_tokens_used,
                    )
            if method == "error" and params.get("turnId") == turn_id:
                # Current app-server nests the public message under ``error``.
                # A retrying notification is informational; the terminal event
                # arrives later and must remain the source of truth.
                if params.get("willRetry") is True:
                    continue
                error = params.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                raise RpcError(str(message or params.get("message") or "Codex turn failed"))

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
