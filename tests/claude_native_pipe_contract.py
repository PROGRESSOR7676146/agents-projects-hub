"""Test-only semantic seam; no route, credentials, callback or durable authority.

The host supplies immutable expectations before the child exists. A returned
prepared response proves local consumption only, never native execution or replay
permission. The production BridgeAttemptGate deliberately remains unchanged.
"""

from __future__ import annotations

import hashlib
import re
import struct
import threading
from dataclasses import dataclass, field
from time import monotonic as _monotonic
from typing import NoReturn

from hermes_codex_router.review_bridge_protocol import BridgeFrame, BridgeFrameType
from hermes_codex_router.review_materials import decode_review_capsule
from tests.claude_native_request_contract import (
    MAX_REQUEST_BYTES,
    SUPPORTED_VERSION,
    ExpectedNativeRequest,
    NativeRequestContractError,
    validate_headers,
    validate_request_body,
)

MAX_HEADER_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_PROMPT_PREFIX = "Review only this fictional capsule.\n"


class NativePipeContractError(ValueError):
    """Fixed fixture diagnostics only; no received bytes or exception chains."""


def _invalid(code: str = "example_native_pipe_request_invalid") -> NoReturn:
    raise NativePipeContractError(code)


@dataclass(frozen=True, slots=True)
class NativeRequestEnvelope:
    raw_headers: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)


def encode_native_request(headers: bytes, body: bytes) -> bytes:
    """Bounded packaging, preserving malformed HTTP for host-side refusal."""
    if (
        type(headers) is not bytes
        or not 0 < len(headers) <= MAX_HEADER_BYTES
        or type(body) is not bytes
        or not 0 < len(body) <= MAX_REQUEST_BYTES
    ):
        _invalid()
    return struct.pack("!I", len(headers)) + headers + body


def decode_native_request(payload: bytes) -> NativeRequestEnvelope:
    """Exact CRLF POST framing; duplicates remain except ambiguous lengths."""
    if (
        type(payload) is not bytes
        or not 4 < len(payload) <= 4 + MAX_HEADER_BYTES + MAX_REQUEST_BYTES
    ):
        _invalid()
    size = struct.unpack("!I", payload[:4])[0]
    if not 0 < size <= MAX_HEADER_BYTES:
        _invalid("example_native_pipe_header_bound")
    if not 0 < len(payload) - 4 - size <= MAX_REQUEST_BYTES:
        _invalid()
    headers, body = payload[4 : 4 + size], payload[4 + size :]
    if not headers.endswith(b"\r\n\r\n"):
        _invalid()
    lines = headers[:-4].split(b"\r\n")
    if (
        lines[0]
        not in (
            b"POST /v1/messages HTTP/1.1",
            b"POST /v1/messages?beta=true HTTP/1.1",
        )
        or not 1 <= len(lines) - 1 <= 24
    ):
        _invalid()
    pairs: list[tuple[str, str]] = []
    length: int | None = None
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        if (
            not separator
            or re.fullmatch(rb"[A-Za-z0-9-]{1,64}", name) is None
            or len(value) > 512
            or any(char < 32 or char >= 127 for char in value)
            or value != value.strip(b" ")
        ):
            _invalid()
        key = name.decode("ascii")
        entry = value.decode("ascii")
        if key.lower() == "transfer-encoding":
            _invalid()
        if key.lower() == "content-length":
            if length is not None or re.fullmatch(r"[1-9][0-9]{0,4}", entry) is None:
                _invalid()
            length = int(entry)
        pairs.append((key, entry))
    if length != len(body):
        _invalid()
    return NativeRequestEnvelope(headers, tuple(pairs), body)


def native_pipe_prompt(capsule: bytes) -> str:
    """The same fixed derivation runs on host selection and received CAPSULE."""
    if type(capsule) is not bytes or not 0 < len(capsule) <= MAX_REQUEST_BYTES:
        _invalid("example_native_pipe_material_invalid")
    prompt: str | None = None
    try:
        decode_review_capsule(capsule, hashlib.sha256(capsule).hexdigest())
        candidate = _PROMPT_PREFIX + capsule.decode("utf-8")
        if len(candidate.encode("utf-8")) <= MAX_REQUEST_BYTES:
            prompt = candidate
    except (ValueError, UnicodeError):
        pass
    if prompt is None:
        _invalid("example_native_pipe_material_invalid")
    return prompt


@dataclass(frozen=True, slots=True)
class NativePipeObservation:
    attempted: bool
    retired: bool
    revoked: bool


class NativePipeAttempt:
    """Serialized one-use prepared fake response, explicitly fixture-only."""

    def __init__(
        self,
        capsule: bytes,
        expected: ExpectedNativeRequest,
        *,
        port: int,
        case: str,
        response: bytes,
        timeout_seconds: float = 75,
    ) -> None:
        prompt = native_pipe_prompt(capsule)
        if (
            type(expected) is not ExpectedNativeRequest
            or type(getattr(expected, "version", None)) is not str
            or getattr(expected, "version", None) != SUPPORTED_VERSION
            or type(getattr(expected, "environment", None)) is not str
            or not 0 < len(expected.environment) <= 2048
            or type(getattr(expected, "prompt", None)) is not str
            or expected.prompt != prompt
            or type(port) is not int
            or not 0 < port <= 65535
            or type(case) is not str
            or case not in {"bearer-success", "api-key-success"}
            or type(response) is not bytes
            or not 0 < len(response) <= _MAX_RESPONSE_BYTES
            or type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 105
        ):
            _invalid("example_native_pipe_configuration_invalid")
        now = _monotonic()
        if type(now) not in (int, float) or not 0 <= now <= 1e12 or now + timeout_seconds <= now:
            _invalid("example_native_pipe_clock_invalid")
        self._expected = ExpectedNativeRequest(expected.version, expected.environment, prompt)
        self._capsule, self._response = capsule, response
        self._port, self._case = port, case
        self._deadline, self._last_clock = now + timeout_seconds, now
        self._lock = threading.Lock()
        self._attempted = self._retired = self._revoked = False

    @property
    def capsule_bytes(self) -> bytes:
        return self._capsule

    @property
    def observation(self) -> NativePipeObservation:
        with self._lock:
            return NativePipeObservation(self._attempted, self._retired, self._revoked)

    def cancel(self) -> None:
        with self._lock:
            self._retired = self._revoked = True

    def close(self) -> None:
        self.cancel()

    def submit(self, frame: object) -> bytes:
        with self._lock:
            if self._retired:
                _invalid("example_native_pipe_retired")
            # Retire before any validation; a malformed first frame cannot
            # regain response eligibility. Attempted means actual consumption.
            self._retired = True
            if type(frame) is not BridgeFrame:
                _invalid()
            kind, payload = getattr(frame, "kind", None), getattr(frame, "payload", None)
            if kind is not BridgeFrameType.REQUEST or not isinstance(payload, bytes):
                _invalid()
            valid = False
            try:
                envelope = decode_native_request(payload)
                size = validate_headers(
                    envelope.headers, port=self._port, case=self._case, method="POST"
                )
                if size == len(envelope.body):
                    validate_request_body(envelope.body, self._expected)
                    valid = True
            except (NativeRequestContractError, NativePipeContractError):
                pass
            if not valid:
                _invalid()
            now = _monotonic()
            if type(now) not in (int, float) or not self._last_clock <= now < self._deadline:
                _invalid("example_native_pipe_expired")
            self._last_clock = now
            self._attempted = True
            return self._response
