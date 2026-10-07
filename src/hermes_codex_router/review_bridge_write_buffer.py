"""Bounded offline output and partial-write accounting, without I/O callbacks."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import NoReturn

from .review_bridge_protocol import (
    MAX_BRIDGE_FEED_BYTES,
    MAX_BRIDGE_FRAMES,
    MAX_BRIDGE_TOTAL_BYTES,
    BridgeFrame,
    BridgeFrameError,
    BridgeFrameType,
    encode_bridge_frame,
)


class BridgeWriteError(RuntimeError):
    """Fixed local buffer diagnostic; no callback, wire or response data."""


@dataclass(frozen=True, slots=True)
class BridgeWriteObservation:
    pending_bytes: int
    admitted_frames: int
    admitted_bytes: int
    advanced_bytes: int
    cancelled: bool
    failed: bool


def bounded_response_frames(headers: bytes, body: bytes) -> tuple[BridgeFrame, ...]:
    """Frame exact fixture bytes; do not interpret HTTP/SSE or native evidence."""
    if (
        type(headers) is not bytes
        or len(headers) > 16 * 1024
        or type(body) is not bytes
        or len(body) > 1024 * 1024
    ):
        raise BridgeWriteError("bridge_write_response_bound")
    return (
        BridgeFrame(BridgeFrameType.RESPONSE_HEADERS, headers),
        *(
            BridgeFrame(BridgeFrameType.RESPONSE_CHUNK, body[offset : offset + 65536])
            for offset in range(0, len(body), 65536)
        ),
        BridgeFrame(BridgeFrameType.RESPONSE_END, b""),
    )


class BridgeWriteBuffer:
    """One serialized owner offers bytes and accounts caller-reported writes.

    Admission is not a write; advance is not peer receipt or provider evidence.
    This has no writer/iterator/thread and cannot bound a blocking I/O operation.
    A future I/O owner must enforce nonblocking I/O, deadline and channel cleanup.
    Cancel aborts the whole buffer; if a frame was partly sent, close the pipe.
    Never append wire CANCEL or another frame to a truncated frame suffix.
    """

    def __init__(
        self,
        *,
        capacity: int = 2 * 1024 * 1024,
        max_frames: int = MAX_BRIDGE_FRAMES,
        max_bytes: int = MAX_BRIDGE_TOTAL_BYTES,
    ) -> None:
        if (
            type(capacity) is not int
            or not 1 <= capacity <= MAX_BRIDGE_TOTAL_BYTES
            or type(max_frames) is not int
            or not 1 <= max_frames <= MAX_BRIDGE_FRAMES
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_BRIDGE_TOTAL_BYTES
        ):
            raise BridgeWriteError("bridge_write_budget_invalid")
        self._capacity, self._max_frames, self._max_bytes = capacity, max_frames, max_bytes
        self._queue: deque[bytes] = deque()
        self._offset = self._offer = self._pending = 0
        self._frames = self._bytes = self._advanced = 0
        self._cancelled = self._failed = False

    @property
    def observation(self) -> BridgeWriteObservation:
        return BridgeWriteObservation(
            self._pending, self._frames, self._bytes, self._advanced, self._cancelled, self._failed
        )

    def _open(self) -> None:
        if self._failed or self._cancelled:
            raise BridgeWriteError("bridge_write_retired")

    def _fail(self, code: str) -> NoReturn:
        self._failed = True
        self._queue.clear()
        self._pending = self._offset = self._offer = 0
        raise BridgeWriteError(code) from None

    def enqueue(self, frame: BridgeFrame) -> bool:
        self._open()
        if type(frame) is not BridgeFrame:
            self._fail("bridge_write_frame_invalid")
        kind, payload = frame.kind, frame.payload
        wire: bytes | None = None
        try:
            wire = encode_bridge_frame(BridgeFrame(kind, payload))
        except BridgeFrameError:
            pass
        if wire is None:
            self._fail("bridge_write_frame_invalid")
        size = len(wire)
        if size > self._capacity:
            self._fail("bridge_write_frame_capacity")
        if self._frames >= self._max_frames or self._bytes + size > self._max_bytes:
            self._fail("bridge_write_lifetime_budget")
        if self._pending + size > self._capacity:
            return False
        self._queue.append(wire)
        self._pending += size
        self._frames += 1
        self._bytes += size
        return True

    def peek(self, *, max_bytes: int = MAX_BRIDGE_FEED_BYTES) -> bytes:
        self._open()
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BRIDGE_FEED_BYTES:
            self._fail("bridge_write_offer_bound")
        if not self._queue:
            return b""
        wire = self._queue[0]
        if self._offer and max_bytes < self._offer:
            self._fail("bridge_write_offer_inflight")
        if not self._offer:
            self._offer = min(max_bytes, len(wire) - self._offset)
        return wire[self._offset : self._offset + self._offer]

    def advance(self, count: int) -> None:
        self._open()
        if type(count) is not int or not self._offer or not 0 <= count <= self._offer:
            self._fail("bridge_write_advance_invalid")
        if count == 0:
            return
        self._offset += count
        self._pending -= count
        self._advanced += count
        self._offer = 0
        if self._offset == len(self._queue[0]):
            self._queue.popleft()
            self._offset = 0

    def cancel(self) -> None:
        self._cancelled = True
        self._queue.clear()
        self._pending = self._offset = self._offer = 0
