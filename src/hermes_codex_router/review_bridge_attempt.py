"""One in-memory attempt to a caller-injected fake upstream, without live wiring.

The trusted caller owns prior authorization and exact expected request bytes.
The hash policy is not a native HTTP request validator or durable replay guard.
Deadlines bound admission/result eligibility; they cannot interrupt a callback.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .review_bridge_protocol import (
    BridgeFrame,
    BridgeFrameError,
    BridgeFrameType,
    validate_bridge_frame,
)
from .review_materials import ReviewCapsule, decode_review_capsule

_MAX_RESPONSE_BYTES = 64 * 1024
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BridgeAttemptError(RuntimeError):
    """A fixed local diagnostic, without callback or material details."""


@dataclass(frozen=True, slots=True, repr=False)
class BridgeAttemptSpec:
    attempt_id: str
    material_binding: str
    capsule_sha256: str
    capsule_size: int
    native_session_id: str
    model: str
    effort: str
    max_output_tokens: int
    expected_request_sha256: str


class BridgeAttemptState(Enum):
    READY = "ready"
    CLOSED = "closed"
    CONSUMED = "consumed"
    CALLBACK_RETURNED = "callback_returned"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class BridgeAttemptObservation:
    state: BridgeAttemptState
    attempted: bool
    revoked: bool


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _validate_spec(spec: BridgeAttemptSpec) -> None:
    if type(spec) is not BridgeAttemptSpec or (
        not _matches(_OPAQUE, spec.attempt_id)
        or not _matches(_OPAQUE, spec.material_binding)
        or not _matches(_SHA256, spec.capsule_sha256)
        or not _matches(_SHA256, spec.expected_request_sha256)
        or not _matches(_MODEL, spec.model)
        or type(spec.effort) is not str
        or spec.effort not in {"low", "medium", "high", "xhigh", "max"}
        or type(spec.capsule_size) is not int
        or not 1 <= spec.capsule_size <= 1024 * 1024
        or type(spec.max_output_tokens) is not int
        or not 1 <= spec.max_output_tokens <= 4096
    ):
        raise BridgeAttemptError("bridge_attempt_spec_invalid")
    valid_uuid = False
    if type(spec.native_session_id) is str and len(spec.native_session_id) == 36:
        try:
            identity = uuid.UUID(spec.native_session_id)
            valid_uuid = str(identity) == spec.native_session_id and identity.int != 0
        except ValueError:
            pass
    if not valid_uuid:
        raise BridgeAttemptError("bridge_attempt_identity_invalid")


def _clock_value(clock: Callable[[], float]) -> float:
    value: object = None
    try:
        value = clock()
    except BaseException:
        value = None
    # Range comparisons also refuse NaN/inf and avoid converting giant ints.
    if (
        not isinstance(value, (int, float))
        or type(value) not in (int, float)
        or not 0 <= value <= 1e12
    ):
        raise BridgeAttemptError("bridge_attempt_clock_invalid")
    return float(value)


class BridgeAttemptGate:
    """Claim once before callback, never issue a reusable permission ticket.

    This gate has no provider client, routing, environment, process or DB access.
    The injected upstream is trusted fake-fixture code. Creating another gate
    after a crash does not inherit this one's consumed state. Future durable
    workflow admission and an owned bounded I/O pump remain separate owners.
    """

    def __init__(
        self,
        spec: BridgeAttemptSpec,
        capsule: ReviewCapsule,
        *,
        upstream: Callable[[bytes], bytes],
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 300,
    ) -> None:
        _validate_spec(spec)
        if type(capsule) is not ReviewCapsule:
            raise BridgeAttemptError("bridge_attempt_capsule_type_invalid")
        if (
            not callable(upstream)
            or not callable(clock)
            or type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 300
        ):
            raise BridgeAttemptError("bridge_attempt_configuration_invalid")
        now = _clock_value(clock)
        deadline = now + timeout_seconds
        if deadline <= now:
            raise BridgeAttemptError("bridge_attempt_configuration_invalid")
        data: bytes | None = None
        try:
            captured = capsule.read()
            document = decode_review_capsule(captured, spec.capsule_sha256)
            if (
                type(captured) is bytes
                and len(captured) == spec.capsule_size == capsule.size
                and capsule.digest == spec.capsule_sha256
                and document["binding"] == spec.material_binding
            ):
                data = captured
        except (ValueError, OSError):
            pass
        if data is None:
            raise BridgeAttemptError("bridge_attempt_material_invalid")
        self._request_digest, self._capsule_bytes = spec.expected_request_sha256, data
        self._upstream, self._clock = upstream, clock
        self._deadline, self._last_clock = deadline, now
        self._lock = threading.Lock()
        self._state = BridgeAttemptState.READY
        self._attempted = self._revoked = False

    @property
    def capsule_bytes(self) -> bytes:
        return self._capsule_bytes

    @property
    def observation(self) -> BridgeAttemptObservation:
        with self._lock:
            return BridgeAttemptObservation(self._state, self._attempted, self._revoked)

    def cancel(self) -> None:
        with self._lock:
            self._revoked = True
            if self._state is BridgeAttemptState.READY:
                self._state = BridgeAttemptState.CLOSED

    def close(self) -> None:
        self.cancel()

    def _check_time(self) -> None:
        now = _clock_value(self._clock)
        if now < self._last_clock:
            raise BridgeAttemptError("bridge_attempt_clock_invalid")
        self._last_clock = now
        if now >= self._deadline:
            raise BridgeAttemptError("bridge_attempt_expired")

    def submit(self, frame: BridgeFrame) -> bytes:
        with self._lock:
            if self._state is not BridgeAttemptState.READY:
                raise BridgeAttemptError("bridge_attempt_retired")
            # Every refusal before claim retires the local attempt. Framing
            # cannot accept child SPEC/CAPSULE or a response as host authority.
            self._state = BridgeAttemptState.CLOSED
            if type(frame) is not BridgeFrame:
                raise BridgeAttemptError("bridge_attempt_request_invalid")
            # A caller can bypass dataclass freeze. Validate, hash and pass
            # the same immutable local bytes; never re-read the caller frame.
            kind, payload = frame.kind, frame.payload
            try:
                validate_bridge_frame(BridgeFrame(kind, payload))
            except BridgeFrameError:
                raise BridgeAttemptError("bridge_attempt_request_invalid") from None
            if (
                kind is not BridgeFrameType.REQUEST
                or hashlib.sha256(payload).hexdigest() != self._request_digest
            ):
                raise BridgeAttemptError("bridge_attempt_request_invalid")
            self._check_time()
            self._attempted = True
            self._state = BridgeAttemptState.CONSUMED
        # Release the lock before the callback; reentrant/concurrent submit
        # already observes CONSUMED. Cancellation now cannot promise zero calls.
        callback_failed = False
        response: object = None
        try:
            response = self._upstream(payload)
        except BaseException:
            callback_failed = True
        with self._lock:
            self._state = BridgeAttemptState.UNCERTAIN
            if callback_failed:
                # Raise outside the handler: raw callback exceptions cannot
                # remain reachable through __cause__ or __context__.
                raise BridgeAttemptError("bridge_attempt_upstream_uncertain")
            if type(response) is not bytes or len(response) > _MAX_RESPONSE_BYTES:
                raise BridgeAttemptError("bridge_attempt_response_invalid")
            if self._revoked:
                raise BridgeAttemptError("bridge_attempt_revoked_after_claim")
            self._check_time()
            self._state = BridgeAttemptState.CALLBACK_RETURNED
            return response
