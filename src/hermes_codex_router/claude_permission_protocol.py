"""Small, strict value types shared by the native hook and protected transport."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class PermissionProtocolError(ValueError):
    """Invalid permission data; messages intentionally contain no input."""


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_IDENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise PermissionProtocolError("invalid UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise PermissionProtocolError("invalid UUID") from exc
    if str(parsed) != value:
        raise PermissionProtocolError("noncanonical UUID")
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise PermissionProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise PermissionProtocolError("invalid JSON number")


def _validate_unicode(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise PermissionProtocolError("invalid Unicode")
    elif isinstance(value, list):
        for item in value:
            _validate_unicode(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_unicode(key)
            _validate_unicode(item)


def parse_json_strict(raw: str | bytes, *, max_bytes: int = 131072) -> Any:
    try:
        if isinstance(raw, bytes):
            if len(raw) > max_bytes:
                raise PermissionProtocolError("JSON too large")
            raw = raw.decode("utf-8", "strict")
        elif len(raw.encode("utf-8")) > max_bytes:
            raise PermissionProtocolError("JSON too large")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        _validate_unicode(value)
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise PermissionProtocolError("invalid JSON") from exc


def canonical_json(value: Any, *, max_bytes: int = 131072) -> str:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        _validate_unicode(value)
        if len(raw.encode("utf-8")) > max_bytes:
            raise PermissionProtocolError("JSON too large")
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PermissionProtocolError("invalid JSON") from exc


def event_digest(event: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(event, max_bytes=65536).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProtectedPayload:
    request_nonce: str
    job_id: str
    session_id: str
    generation: int
    root_digest: str
    lease_id: str
    launch_epoch: str
    tool_name: str
    tool_input: Any
    expires_at: int

    def __post_init__(self) -> None:
        canonical_uuid(self.request_nonce)
        canonical_uuid(self.session_id)
        canonical_uuid(self.lease_id)
        canonical_uuid(self.launch_epoch)
        if not isinstance(self.job_id, str) or not _IDENT.fullmatch(self.job_id):
            raise PermissionProtocolError("invalid job ID")
        if type(self.generation) is not int or not 1 <= self.generation <= 2**53 - 1:
            raise PermissionProtocolError("invalid generation")
        if not isinstance(self.root_digest, str) or not _HEX64.fullmatch(self.root_digest):
            raise PermissionProtocolError("invalid root digest")
        if not isinstance(self.tool_name, str) or not _IDENT.fullmatch(self.tool_name):
            raise PermissionProtocolError("invalid tool name")
        if type(self.expires_at) is not int or not 1 <= self.expires_at <= 2**53 - 1:
            raise PermissionProtocolError("invalid expiry")
        canonical_json(self.tool_input, max_bytes=65536)

    def to_json(self) -> str:
        return canonical_json(
            {
                "version": 1,
                "requestNonce": self.request_nonce,
                "jobId": self.job_id,
                "sessionId": self.session_id,
                "generation": self.generation,
                "rootDigest": self.root_digest,
                "leaseId": self.lease_id,
                "launchEpoch": self.launch_epoch,
                "writer": "telegram",
                "toolName": self.tool_name,
                "input": self.tool_input,
                "expiresAt": self.expires_at,
            },
            max_bytes=65536,
        )

    @classmethod
    def parse(cls, raw: str) -> ProtectedPayload:
        value = parse_json_strict(raw, max_bytes=65536)
        required = {
            "version",
            "requestNonce",
            "jobId",
            "sessionId",
            "generation",
            "rootDigest",
            "leaseId",
            "launchEpoch",
            "writer",
            "toolName",
            "input",
            "expiresAt",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["writer"] != "telegram"
        ):
            raise PermissionProtocolError("invalid protected payload")
        return cls(
            value["requestNonce"],
            value["jobId"],
            value["sessionId"],
            value["generation"],
            value["rootDigest"],
            value["leaseId"],
            value["launchEpoch"],
            value["toolName"],
            value["input"],
            value["expiresAt"],
        )
