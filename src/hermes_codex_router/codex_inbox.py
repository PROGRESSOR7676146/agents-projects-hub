"""Bounded FIFO storage; terminal state never competes for a message slot."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Callable, Generic, TypeVar

from .codex_rpc import RpcError

T = TypeVar("T")


class BoundedInbox(Generic[T]):
    def __init__(
        self,
        *,
        max_frames: int = 1024,
        max_bytes: int = 8 * 1024 * 1024,
        max_frame_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not 1 <= max_frames <= 1024 or not 1 <= max_frame_bytes <= max_bytes:
            raise ValueError("invalid Codex inbox bounds")
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._max_frame_bytes = max_frame_bytes
        self._frames: deque[tuple[T, int]] = deque()
        self._bytes = 0
        self._terminal: BaseException | None = None
        self._condition = threading.Condition()
        self._capacity_callback: Callable[[], None] | None = None

    @property
    def pending_frames(self) -> int:
        with self._condition:
            return len(self._frames)

    @property
    def pending_bytes(self) -> int:
        with self._condition:
            return self._bytes

    @property
    def terminal(self) -> BaseException | None:
        with self._condition:
            return self._terminal

    def set_capacity_callback(self, callback: Callable[[], None] | None) -> None:
        with self._condition:
            self._capacity_callback = callback

    def try_put(self, value: T, size: int) -> bool:
        if not 0 <= size <= self._max_frame_bytes:
            raise RpcError("Codex inbound frame exceeded its bound")
        with self._condition:
            if self._terminal is not None:
                raise self._terminal
            if len(self._frames) >= self._max_frames or self._bytes + size > self._max_bytes:
                return False
            self._frames.append((value, size))
            self._bytes += size
            self._condition.notify_all()
            return True

    def finish(self, error: BaseException) -> None:
        with self._condition:
            if self._terminal is None:
                self._terminal = error
            self._condition.notify_all()
            callback = self._capacity_callback
        if callback is not None:
            callback()

    def put(self, value: T, size: int) -> None:
        """Wait for capacity; finish wakes blocked producers as well as consumers."""
        with self._condition:
            while not self.try_put(value, size):
                self._condition.wait()

    def get(self, *, timeout: float) -> T:
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while not self._frames:
                if self._terminal is not None:
                    raise self._terminal
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            value, size = self._frames.popleft()
            self._bytes -= size
            self._condition.notify_all()
            callback = self._capacity_callback
        if callback is not None:
            callback()
        return value
