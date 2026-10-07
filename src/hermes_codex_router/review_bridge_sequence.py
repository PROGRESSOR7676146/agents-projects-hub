"""Single-owner offline wire ordering, without invocation or publication policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

from .review_bridge_protocol import (
    MAX_BRIDGE_FRAMES,
    MAX_BRIDGE_TOTAL_BYTES,
    BridgeFrame,
    BridgeFrameError,
    BridgeFrameType,
    validate_bridge_frame,
)


class BridgeSequenceError(RuntimeError):
    """Fixed local ordering diagnostic; no wire payload or native meaning."""


class BridgeDirection(Enum):
    HOST_TO_CHILD = "host_to_child"
    CHILD_TO_HOST = "child_to_host"


class BridgeDisposition(Enum):
    OBSERVED = "observed"
    DISCARD_STDOUT = "discard_stdout"


@dataclass(frozen=True, slots=True)
class BridgeSequenceObservation:
    request_seen: bool
    response_ended: bool
    cancelled: bool
    exit_seen: bool
    failed: bool
    host_eof: bool
    child_eof: bool

    @property
    def transport_closed(self) -> bool:
        return self.host_eof and self.child_eof and not self.failed

    @property
    def response_incomplete(self) -> bool:
        return self.exit_seen and self.request_seen and not self.response_ended


class BridgeSequence:
    """Call only from one serialized owner; observations attest wire order only.

    Request observation is not a consumed callback or native turn acceptance.
    An early exit can close the pipes without proving native refusal/success.
    After CANCEL, in-flight stdout drains within its original bound and MUST
    be discarded by the future publication owner. No reset or replay ticket.
    """

    def __init__(self, *, max_frames: int = 1024, max_bytes: int = 3 * 1024 * 1024) -> None:
        if (
            type(max_frames) is not int
            or not 1 <= max_frames <= MAX_BRIDGE_FRAMES
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_BRIDGE_TOTAL_BYTES
        ):
            raise BridgeSequenceError("bridge_sequence_budget_invalid")
        self._max_frames, self._max_bytes = max_frames, max_bytes
        self._frames = dict.fromkeys(BridgeDirection, 0)
        self._bytes = dict.fromkeys(BridgeDirection, 0)
        self._eof = dict.fromkeys(BridgeDirection, False)
        self._spec = self._capsule = self._request = self._headers = False
        self._end = self._cancelled = self._exit = self._failed = False
        self._response_bytes = self._stdout_bytes = 0

    @property
    def observation(self) -> BridgeSequenceObservation:
        return BridgeSequenceObservation(
            self._request,
            self._end,
            self._cancelled,
            self._exit,
            self._failed,
            self._eof[BridgeDirection.HOST_TO_CHILD],
            self._eof[BridgeDirection.CHILD_TO_HOST],
        )

    def _fail(self, code: str) -> NoReturn:
        self._failed = True
        raise BridgeSequenceError(code) from None

    def _direction(self, direction: BridgeDirection) -> None:
        if self._failed:
            raise BridgeSequenceError("bridge_sequence_retired")
        if type(direction) is not BridgeDirection:
            self._fail("bridge_sequence_direction_invalid")
        if self._eof[direction]:
            self._fail("bridge_sequence_direction_closed")

    def observe(self, direction: BridgeDirection, frame: BridgeFrame) -> BridgeDisposition:
        self._direction(direction)
        if type(frame) is not BridgeFrame:
            self._fail("bridge_sequence_frame_invalid")
        kind, payload = frame.kind, frame.payload
        valid = True
        try:
            validate_bridge_frame(BridgeFrame(kind, payload))
        except BridgeFrameError:
            valid = False
        if not valid:
            self._fail("bridge_sequence_frame_invalid")
        size = len(payload)
        wire_size = 9 + size
        if self._frames[direction] >= self._max_frames:
            self._fail("bridge_sequence_frame_budget")
        if self._bytes[direction] + wire_size > self._max_bytes:
            self._fail("bridge_sequence_byte_budget")
        if self._exit:
            self._fail("bridge_sequence_after_exit")
        disposition = BridgeDisposition.OBSERVED
        allowed = False
        if direction is BridgeDirection.HOST_TO_CHILD:
            if kind is BridgeFrameType.CANCEL:
                allowed = not self._cancelled
            elif not self._cancelled:
                if kind is BridgeFrameType.SPEC:
                    allowed = not self._spec
                elif kind is BridgeFrameType.CAPSULE:
                    allowed = self._spec and not self._capsule
                elif kind is BridgeFrameType.RESPONSE_HEADERS:
                    allowed = self._request and not self._headers
                elif kind is BridgeFrameType.RESPONSE_CHUNK:
                    allowed = self._headers and not self._end
                elif kind is BridgeFrameType.RESPONSE_END:
                    allowed = self._headers and not self._end
        elif kind is BridgeFrameType.NATIVE_EXIT:
            allowed = self._capsule or self._cancelled
        elif self._capsule:
            if kind is BridgeFrameType.REQUEST:
                allowed = not self._request and not self._cancelled
            elif kind is BridgeFrameType.NATIVE_STDOUT:
                allowed = True
                if self._cancelled:
                    disposition = BridgeDisposition.DISCARD_STDOUT
        if not allowed:
            self._fail("bridge_sequence_order_invalid")
        if (
            kind is BridgeFrameType.RESPONSE_CHUNK and self._response_bytes + size > 1024 * 1024
        ) or (kind is BridgeFrameType.NATIVE_STDOUT and self._stdout_bytes + size > 256 * 1024):
            self._fail("bridge_sequence_stream_budget")
        self._frames[direction] += 1
        self._bytes[direction] += wire_size
        if kind is BridgeFrameType.SPEC:
            self._spec = True
        elif kind is BridgeFrameType.CAPSULE:
            self._capsule = True
        elif kind is BridgeFrameType.REQUEST:
            self._request = True
        elif kind is BridgeFrameType.RESPONSE_HEADERS:
            self._headers = True
        elif kind is BridgeFrameType.RESPONSE_CHUNK:
            self._response_bytes += size
        elif kind is BridgeFrameType.RESPONSE_END:
            self._end = True
        elif kind is BridgeFrameType.NATIVE_STDOUT:
            self._stdout_bytes += size
        elif kind is BridgeFrameType.CANCEL:
            self._cancelled = True
        elif kind is BridgeFrameType.NATIVE_EXIT:
            self._exit = True
        return disposition

    def finish(self, direction: BridgeDirection) -> None:
        self._direction(direction)
        permitted = (
            self._end or self._cancelled or self._exit
            if direction is BridgeDirection.HOST_TO_CHILD
            else self._exit
        )
        if not permitted:
            self._fail("bridge_sequence_eof_incomplete")
        self._eof[direction] = True
