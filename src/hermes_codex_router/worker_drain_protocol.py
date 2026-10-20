"""Bounded, process-local drain messages; no provider, SQLite or service authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .release_identity import CURRENT_RELEASE, ReleaseIdentity

FRAME_LIMIT = 4096
PROTOCOL_VERSION = 2
EVIDENCE_SCOPE = "tracked_operations_and_socket_transport_threads"
MAX_REQUEST_LIFETIME_NS = 60_000_000_000


class ControlError(RuntimeError):
    """A local control observation or request is unconfirmed."""


def _integer(value: object) -> bool:
    return type(value) is int and value > 0


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def process_start_ticks(pid: int) -> int:
    try:
        # comm may contain spaces and ')' characters; field 22 follows the last ')'.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        value = int(fields[19])
        if value <= 0:
            raise ValueError
        return value
    except (OSError, ValueError, IndexError):
        raise ControlError("process_identity_unavailable") from None


def boot_identity() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        raise ControlError("boot_identity_unavailable") from None


@dataclass(frozen=True, slots=True)
class ControlBinding:
    worker_id: str
    pid: int
    start_ticks: int
    boot_id: str
    instance_nonce: str
    release: ReleaseIdentity

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.worker_id) is None
            or not _integer(self.pid)
            or not _integer(self.start_ticks)
            or not isinstance(self.boot_id, str)
            or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", self.boot_id) is None
            or not _hex(self.instance_nonce, 32)
            or not isinstance(self.release, ReleaseIdentity)
            or not isinstance(self.release.package_version, str)
            or not self.release.verified
            or type(self.release.clean_tree) is not bool
        ):
            raise ControlError("invalid_binding")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def parse(cls, value: object) -> ControlBinding:
        if not isinstance(value, dict) or set(value) != {
            "worker_id",
            "pid",
            "start_ticks",
            "boot_id",
            "instance_nonce",
            "release",
        }:
            raise ControlError("invalid_binding")
        release = value["release"]
        if not isinstance(release, dict) or set(release) != {
            "package_version",
            "git_sha",
            "built_at",
            "clean_tree",
        }:
            raise ControlError("invalid_binding")
        try:
            return cls(
                value["worker_id"],
                value["pid"],
                value["start_ticks"],
                value["boot_id"],
                value["instance_nonce"],
                ReleaseIdentity(**release),
            )
        except (TypeError, ValueError):
            raise ControlError("invalid_binding") from None

    def require_live_process(self, pid: int) -> None:
        if (
            pid != self.pid
            or process_start_ticks(pid) != self.start_ticks
            or boot_identity() != self.boot_id
        ):
            raise ControlError("process_binding_changed")


def current_binding(
    worker_id: str, instance_nonce: str, *, release: ReleaseIdentity = CURRENT_RELEASE
) -> ControlBinding:
    return ControlBinding(
        worker_id,
        os.getpid(),
        process_start_ticks(os.getpid()),
        boot_identity(),
        instance_nonce,
        release,
    )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ControlError("duplicate_key")
        result[key] = value
    return result


def decode_frame(data: bytes) -> dict[str, object]:
    if len(data) > FRAME_LIMIT or not data.endswith(b"\n") or b"\n" in data[:-1]:
        raise ControlError("invalid_frame")
    return decode_document(data)


def decode_document(data: bytes) -> dict[str, object]:
    if len(data) > FRAME_LIMIT:
        raise ControlError("frame_too_large")
    try:
        result = json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ControlError("invalid_frame") from None
    if (
        not isinstance(result, dict)
        or type(result.get("version")) is not int
        or result["version"] != PROTOCOL_VERSION
    ):
        raise ControlError("invalid_frame")
    return result


def encode_frame(value: dict[str, object]) -> bytes:
    try:
        result = (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            + b"\n"
        )
    except (TypeError, ValueError, RecursionError):
        raise ControlError("invalid_frame") from None
    if len(result) > FRAME_LIMIT:
        raise ControlError("frame_too_large")
    return result


def request_digest(request: dict[str, object]) -> str:
    validate_request(request)
    if request["operation"] != "drain":
        raise ControlError("invalid_request")
    return hashlib.sha256(encode_frame(request)).hexdigest()


def drain_request(
    binding: ControlBinding, *, request_id: str, expires_ns: int
) -> dict[str, object]:
    request: dict[str, object] = {
        "version": PROTOCOL_VERSION,
        "operation": "drain",
        "binding": binding.as_dict(),
        "request_id": request_id,
        "expires_ns": expires_ns,
    }
    validate_request(request)
    return request


def status_request(request: dict[str, object]) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "operation": "status",
        "binding": request["binding"],
        "request_id": request["request_id"],
        "request_digest": request_digest(request),
    }


def validate_request(request: dict[str, object]) -> None:
    if type(request.get("version")) is not int or request["version"] != PROTOCOL_VERSION:
        raise ControlError("invalid_request")
    operation = request.get("operation")
    if operation == "inspect" and set(request) == {"version", "operation"}:
        return
    field = "expires_ns" if operation == "drain" else "request_digest"
    if (
        operation not in ("drain", "status")
        or set(request) != {"version", "operation", "binding", "request_id", field}
        or not _hex(request["request_id"], 32)
        or (operation == "drain" and not _integer(request[field]))
        or (operation == "status" and not _hex(request[field], 64))
    ):
        raise ControlError("invalid_request")
    ControlBinding.parse(request["binding"])
