from __future__ import annotations

import asyncio
import json
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import aiohttp

from .codex_inbox import BoundedInbox
from .codex_rpc import RpcError, RpcOutboundUnavailableError

STDIO_FAILURE_POLL_SECONDS = 1.0


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

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        max_pending_frames: int = 1024,
        max_frame_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise RpcError("Codex stdio pipes are unavailable")
        if not 1 <= max_frame_bytes <= 4 * 1024 * 1024:
            raise ValueError("invalid Codex stdio frame bound")
        self._process = process
        self._reader = process.stdout
        self._writer = process.stdin
        self._closed = False
        self._state_lock = threading.Lock()
        self._first_error: BaseException | None = None
        self._writer_failure: RpcOutboundUnavailableError | None = None
        self._max_frame_bytes = max_frame_bytes
        self._inbound: BoundedInbox[str] = BoundedInbox(
            max_frames=max_pending_frames, max_frame_bytes=max_frame_bytes
        )
        self._outbound: BoundedInbox[str] = BoundedInbox(max_frames=16)
        self._writer_thread = threading.Thread(target=self._write_stdin, daemon=True)
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._writer_thread.start()
        self._reader_thread.start()

    def _write_stdin(self) -> None:
        while True:
            try:
                line = self._outbound.get(timeout=20)
            except queue.Empty:
                continue
            except Exception as exc:
                # The queue's recorded terminal is not a physical write fault.
                self._finish(exc)
                return
            with self._state_lock:
                if self._closed or self._first_error is not None:
                    return
            try:
                self._writer.write(line)
                self._writer.flush()
            except Exception as exc:
                self._finish(exc, writer_failed=True)
                return

    @property
    def writer_failure(self) -> RpcOutboundUnavailableError | None:
        with self._state_lock:
            return self._writer_failure

    def _finish(
        self, error: BaseException, *, writer_failed: bool = False, closing: bool = False
    ) -> bool:
        with self._state_lock:
            if closing:
                if self._closed:
                    return False
                self._closed = True
            if self._first_error is None:
                self._first_error = error
            if writer_failed and not self._closed and self._writer_failure is None:
                self._writer_failure = RpcOutboundUnavailableError()
                self._writer_failure.__cause__ = error
            # A broken stdin does not terminate stdout. Let the reader consume
            # its remaining pipe tail, backpressuring until the caller drains it.
            if not writer_failed:
                self._inbound.finish(self._first_error)
            self._outbound.finish(self._first_error)
            return True

    def _read_stdout(self) -> None:
        try:
            # TextIO's limit is in characters. UTF-8 byte admission below also
            # rejects oversized multibyte frames; allocation stays bounded even
            # for a peer that never emits a newline.
            while line := self._reader.readline(self._max_frame_bytes + 1):
                self._inbound.put(line, len(line.encode("utf-8")))
            self._finish(EOFError("Codex app-server closed stdout"))
        except Exception as exc:
            self._finish(exc)

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
        """Admit an immutable frame; the RPC response remains submission evidence."""
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        if size > 4 * 1024 * 1024:
            raise RpcError("Codex outbound frame exceeded its bound")
        with self._state_lock:
            if self._closed:
                raise RpcError("Codex stdio is closed")
            if self._first_error is not None:
                raise RpcOutboundUnavailableError() from self._first_error
            if not self._outbound.try_put(line, size):
                raise RpcError("Codex outbound buffer exceeded its bound")

    def receive(
        self,
        *,
        timeout: float | None = None,
        response_remaining: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, 20.0 if timeout is None else timeout)
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if response_remaining is not None:
                # Run outside transport/queue locks. A caller already waiting
                # on quiet stdout must still notice an asynchronous stdin fault.
                remaining = min(remaining, response_remaining(), STDIO_FAILURE_POLL_SECONDS)
            try:
                line = self._inbound.get(timeout=remaining)
                break
            except queue.Empty as exc:
                if time.monotonic() >= deadline:
                    raise RpcError("timed out waiting for Codex stdio") from exc
        try:
            message = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise RpcError("Codex app-server emitted malformed JSON") from exc
        if not isinstance(message, dict):
            raise RpcError("Codex app-server message must be an object")
        return message

    def close(self) -> None:
        if not self._finish(EOFError("Codex stdio closed"), closing=True):
            return
        try:
            # Stop the owned peer before taking TextIO locks: a writer may be
            # blocked on a full stdin pipe while the peer is blocked on stdout.
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
        finally:
            self._reader_thread.join(timeout=2)
            self._writer_thread.join(timeout=2)
            try:
                if not self._reader_thread.is_alive():
                    self._reader.close()
            finally:
                if not self._writer_thread.is_alive():
                    try:
                        self._writer.close()
                    except BrokenPipeError:
                        pass  # Owned peer already terminated; no further flush is possible.


class UnixWebSocketTransport:
    """Synchronous facade over Codex's WebSocket-over-Unix transport."""

    def __init__(
        self, socket_path: Path, *, timeout: float = 20.0, max_pending_frames: int = 1024
    ) -> None:
        self._socket_path = socket_path.expanduser().resolve(strict=True)
        self._timeout = timeout
        self._outbound: queue.Queue[str] = queue.Queue(maxsize=16)
        self._inbound: BoundedInbox[str] = BoundedInbox(max_frames=max_pending_frames)
        self._receiver_done = threading.Event()
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
        first = self._inbound.terminal
        if first is not None and not self._inbound.pending_frames:
            self.close()
            raise RpcError(f"Codex Unix WebSocket failed: {type(first).__name__}")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_owned())
        except BaseException as exc:
            self._inbound.finish(
                exc if isinstance(exc, Exception) else EOFError("Codex transport thread stopped")
            )
        finally:
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
        capacity_event = asyncio.Event()
        self._outbound_event = outbound_event
        self._inbound.set_capacity_callback(lambda: self._wake_event(capacity_event))
        connector = aiohttp.UnixConnector(path=str(self._socket_path))
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(
                    "http://localhost/", max_msg_size=4 * 1024 * 1024
                ) as websocket:
                    self._ready.set()

                    async def sender() -> None:
                        try:
                            while not self._receiver_done.is_set():
                                outbound_event.clear()
                                try:
                                    message = self._outbound.get_nowait()
                                except queue.Empty:
                                    await outbound_event.wait()
                                    continue
                                await websocket.send_str(message)
                        except Exception as exc:
                            self._inbound.finish(exc)
                            raise

                    async def receiver() -> None:
                        try:
                            async for message in websocket:
                                if message.type == aiohttp.WSMsgType.TEXT:
                                    size = len(message.data.encode("utf-8"))
                                    while True:
                                        # Clear before recheck: a concurrent dequeue cannot
                                        # be lost between observing full and awaiting capacity.
                                        capacity_event.clear()
                                        if self._inbound.try_put(message.data, size):
                                            break
                                        await capacity_event.wait()
                                elif message.type == aiohttp.WSMsgType.ERROR:
                                    raise websocket.exception() or RpcError(
                                        "Codex WebSocket failed"
                                    )
                                else:
                                    raise RpcError("Codex WebSocket frame type is unsupported")
                        except Exception as exc:
                            self._inbound.finish(exc)
                            raise
                        finally:
                            self._inbound.finish(EOFError("Codex WebSocket closed"))
                            self._receiver_done.set()
                            outbound_event.set()

                    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._inbound.set_capacity_callback(None)
            self._outbound_event = None

    def _wake_event(self, event: asyncio.Event | None) -> None:
        loop = self._loop
        if loop is not None and event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # Concurrent transport teardown already closed the loop.

    def send(self, message: dict[str, Any]) -> None:
        if self._closed or self._receiver_done.is_set():
            raise RpcError("Codex Unix WebSocket is closed")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
            raise RpcError("Codex outbound frame exceeded its bound")
        try:
            self._outbound.put_nowait(encoded)
        except queue.Full as exc:
            raise RpcError("Codex outbound buffer exceeded its bound") from exc
        self._wake_event(self._outbound_event)

    def receive(self, *, timeout: float | None = None) -> dict[str, Any]:
        try:
            value = self._inbound.get(timeout=self._timeout if timeout is None else timeout)
        except queue.Empty as exc:
            raise RpcError("timed out waiting for Codex Unix WebSocket") from exc
        except RpcError:
            raise
        except Exception as exc:
            raise RpcError(f"Codex Unix WebSocket failed: {type(exc).__name__}") from exc
        try:
            message = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise RpcError("Codex WebSocket emitted malformed JSON") from exc
        if not isinstance(message, dict):
            raise RpcError("Codex WebSocket message must be an object")
        return message

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inbound.finish(EOFError("Codex WebSocket closed"))
        self._receiver_done.set()
        self._wake_event(self._outbound_event)
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The loop completed between inspection and notification.
        self._thread.join(timeout=5)
