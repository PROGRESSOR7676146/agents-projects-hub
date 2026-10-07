"""Bounded pipe framing, without authorization, provider or lifecycle policy."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import NoReturn

_HEADER = struct.Struct(">4sBI")
_MAGIC = b"HB01"
MAX_BRIDGE_FEED_BYTES = 64 * 1024
MAX_BRIDGE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_BRIDGE_FRAMES = 4096


class BridgeFrameError(RuntimeError):
    """A fixed diagnostic; never carry untrusted header or payload bytes."""


class BridgeFrameType(IntEnum):
    SPEC = 1
    CAPSULE = 2
    REQUEST = 3
    RESPONSE_HEADERS = 4
    RESPONSE_CHUNK = 5
    RESPONSE_END = 6
    NATIVE_STDOUT = 7
    CANCEL = 8
    NATIVE_EXIT = 9


_PAYLOAD_LIMITS = {
    BridgeFrameType.SPEC: 16 * 1024,
    BridgeFrameType.CAPSULE: 1024 * 1024,
    BridgeFrameType.REQUEST: 1024 * 1024,
    BridgeFrameType.RESPONSE_HEADERS: 16 * 1024,
    BridgeFrameType.RESPONSE_CHUNK: 64 * 1024,
    BridgeFrameType.RESPONSE_END: 0,
    BridgeFrameType.NATIVE_STDOUT: 64 * 1024,
    BridgeFrameType.CANCEL: 0,
    BridgeFrameType.NATIVE_EXIT: 16 * 1024,
}


@dataclass(frozen=True, slots=True, repr=False)
class BridgeFrame:
    kind: BridgeFrameType
    payload: bytes

    def __repr__(self) -> str:
        # Invalid caller objects must not inject raw data into diagnostics either.
        kind = self.kind.name if type(self.kind) is BridgeFrameType else "invalid"
        size = str(len(self.payload)) if type(self.payload) is bytes else "invalid"
        return f"BridgeFrame(kind={kind}, payload_bytes={size})"


def validate_bridge_frame(frame: BridgeFrame) -> None:
    if type(frame) is not BridgeFrame or type(frame.kind) is not BridgeFrameType:
        raise BridgeFrameError("bridge_protocol_frame_type")
    if type(frame.payload) is not bytes:
        raise BridgeFrameError("bridge_protocol_payload_type")
    size = len(frame.payload)
    if size > _PAYLOAD_LIMITS[frame.kind]:
        raise BridgeFrameError("bridge_protocol_payload_bound")


def encode_bridge_frame(frame: BridgeFrame) -> bytes:
    validate_bridge_frame(frame)
    return _HEADER.pack(_MAGIC, frame.kind, len(frame.payload)) + frame.payload


class BridgeFrameDecoder:
    """Incremental byte decoder; callers separately enforce direction/sequence.

    The budgets include headers and zero-byte frames. Reject declared payload
    bounds as soon as the header arrives, before accumulating the body. Failure
    or EOF retires this instance; it cannot authorize a replacement attempt.
    """

    def __init__(
        self, *, max_frames: int = MAX_BRIDGE_FRAMES, max_bytes: int = MAX_BRIDGE_TOTAL_BYTES
    ) -> None:
        if (
            type(max_frames) is not int
            or not 1 <= max_frames <= MAX_BRIDGE_FRAMES
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_BRIDGE_TOTAL_BYTES
        ):
            raise BridgeFrameError("bridge_protocol_budget_invalid")
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._frames = self._received = 0
        self._pending = bytearray()
        self._kind: BridgeFrameType | None = None
        self._size = 0
        self._retired = False

    def _fail(self, code: str) -> NoReturn:
        self._retired = True
        self._pending.clear()
        self._kind = None
        raise BridgeFrameError(code) from None

    def feed(self, chunk: bytes) -> tuple[BridgeFrame, ...]:
        if self._retired:
            raise BridgeFrameError("bridge_protocol_retired")
        if type(chunk) is not bytes:
            self._fail("bridge_protocol_feed_type")
        if len(chunk) > MAX_BRIDGE_FEED_BYTES:
            self._fail("bridge_protocol_feed_bound")
        if self._received + len(chunk) > self._max_bytes:
            self._fail("bridge_protocol_byte_budget")
        self._received += len(chunk)
        self._pending.extend(chunk)
        frames: list[BridgeFrame] = []
        while True:
            if self._kind is None:
                if len(self._pending) < _HEADER.size:
                    break
                magic, code, size = _HEADER.unpack_from(self._pending)
                if magic != _MAGIC:
                    self._fail("bridge_protocol_header_invalid")
                try:
                    kind = BridgeFrameType(code)
                except ValueError:
                    self._fail("bridge_protocol_frame_type")
                if size > _PAYLOAD_LIMITS[kind]:
                    self._fail("bridge_protocol_payload_bound")
                if self._frames >= self._max_frames:
                    self._fail("bridge_protocol_frame_budget")
                # Include body bytes not yet received in the budget admission.
                prior_bytes = self._received - len(self._pending)
                if prior_bytes + _HEADER.size + size > self._max_bytes:
                    self._fail("bridge_protocol_byte_budget")
                del self._pending[: _HEADER.size]
                self._kind, self._size = kind, size
                self._frames += 1
            if len(self._pending) < self._size:
                break
            payload = bytes(self._pending[: self._size])
            del self._pending[: self._size]
            assert self._kind is not None
            frames.append(BridgeFrame(self._kind, payload))
            self._kind = None
            self._size = 0
        return tuple(frames)

    def finish(self) -> None:
        if self._retired:
            raise BridgeFrameError("bridge_protocol_retired")
        if self._pending or self._kind is not None:
            self._fail("bridge_protocol_truncated")
        self._retired = True
