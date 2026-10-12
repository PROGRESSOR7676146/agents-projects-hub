"""Test-only native compatibility expectations; no productive authorization.

The fictional selection is fixed before invocation. Native-generated metadata
never authorizes a request, and this module never updates a bridge attempt spec.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, NoReturn, Sequence

MODEL = "claude-opus-5-5"
NATIVE_SESSION_ID = "00000000-0000-4000-8000-000000000001"
MARKER = "example-native-ok"
DUMMY = "example-fixture-no-real-credential"
MATERIAL_TEXT = "Example selected material.\n"
CAPSULE_BYTES = json.dumps(
    {
        "version": 1,
        "binding": "example-review",
        "files": [
            {
                "name": "example.txt",
                "text": MATERIAL_TEXT,
                "size": len(MATERIAL_TEXT.encode("utf-8")),
                "sha256": hashlib.sha256(MATERIAL_TEXT.encode("utf-8")).hexdigest(),
            }
        ],
    },
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
PROMPT = (
    "Review only this fictional capsule and return example-native-ok.\n"
    + CAPSULE_BYTES.decode("utf-8")
)
SYSTEM_PROMPT = "Return the fixture marker."
VENDOR_SYSTEM = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
SUPPORTED_VERSION = "2.1.285"
MAX_REQUEST_BYTES = 16 * 1024
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 256
POST_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": "claude-cli/" + SUPPORTED_VERSION + " (external, sdk-cli)",
    "x-stainless-arch": "x64",
    "x-stainless-lang": "js",
    "x-stainless-os": "Linux",
    "x-stainless-package-version": "0.127.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v26.3.0",
    "x-stainless-timeout": "600",
    "anthropic-beta": (
        "claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
        "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
        "mid-conversation-system-2026-04-07,per-turn-control-2026-07-01,"
        "mid-conversation-tool-changes-2026-07-01,effort-2025-11-24"
    ),
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "x-app": "cli",
    "connection": "keep-alive",
    "accept-encoding": "gzip, deflate, br, zstd",
}
HEAD_HEADERS = {
    "connection": "keep-alive",
    "user-agent": "Bun/1.4.3",
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br, zstd",
}


class NativeRequestContractError(ValueError):
    """Fixed diagnostics only; received native data is never retained."""


def environment_text(os_version: str, date: str) -> str:
    """Caller-selected native scaffold, independent of the arriving request."""
    return (
        "# Environment\nYou have been invoked in the following environment: \n"
        " - Primary working directory: /workspace/example\n"
        " - Is a git repository: false\n - Platform: linux\n - Shell: unknown\n"
        " - OS Version: " + os_version + "\n\n"
        "You are powered by the model named Opus 5.5. The exact model ID is claude-opus-5-5. "
        "Assistant knowledge cutoff is June 2026.\n\n"
        "<total_tokens>15000000 tokens left</total_tokens>\n\nToday's date is " + date + "."
    )


@dataclass(frozen=True)
class ExpectedNativeRequest:
    version: str
    environment: str = field(repr=False)
    prompt: str = field(default=PROMPT, repr=False)


def _invalid() -> NoReturn:
    raise NativeRequestContractError("native_request_contract_invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    _invalid()


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _invalid()
    return number


def _strict_json(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        _invalid()
    parsed = False
    document: Any = None
    try:
        text = raw.decode("utf-8")
        # Bound nesting before constructing the tree. Brackets in JSON strings
        # do not count; the standard decoder owns syntax validation afterwards.
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    _invalid()
            elif char in "]}":
                depth -= 1
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        pending = [document]
        nodes = 0
        while pending:
            value = pending.pop()
            nodes += 1
            if nodes > MAX_JSON_NODES:
                _invalid()
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                _invalid()
        parsed = True
    except (ValueError, UnicodeError, RecursionError):
        pass
    # Raise outside the handler: no raw JSON exception or decoder text is kept
    # in __cause__/__context__, even when a caller inspects rather than prints it.
    if not parsed:
        _invalid()
    return document


def validate_headers(pairs: Sequence[tuple[str, str]], *, port: int, case: str, method: str) -> int:
    """Preserve duplicates until validation; return bounded declared size."""
    if method not in {"HEAD", "POST"} or type(port) is not int or not 0 < port <= 65535:
        _invalid()
    if case not in {"api-key-success", "api-key-reject", "bearer-success", "bearer-reject"}:
        _invalid()
    headers: dict[str, str] = {}
    if len(pairs) > 24:
        _invalid()
    for key, value in pairs:
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", key)
            or not isinstance(value, str)
            or len(value) > 512
            or not value.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or key.lower() in headers
        ):
            _invalid()
        headers[key.lower()] = value
    expected = dict(HEAD_HEADERS if method == "HEAD" else POST_HEADERS)
    expected["host"] = "127.0.0.1:" + str(port)
    length = headers.get("content-length")
    if method == "HEAD":
        if length is not None:
            expected["content-length"] = "0"
        if headers != expected:
            _invalid()
        return 0
    expected["x-claude-code-session-id"] = NATIVE_SESSION_ID
    expected["authorization" if case.startswith("bearer") else "x-api-key"] = (
        "Bearer " + DUMMY if case.startswith("bearer") else DUMMY
    )
    if not isinstance(length, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", length):
        _invalid()
    size = int(length)
    if size > MAX_REQUEST_BYTES:
        _invalid()
    expected["content-length"] = length
    if headers != expected:
        _invalid()
    return size


def validate_request_body(raw: bytes, expected: ExpectedNativeRequest) -> None:
    """Exact selected materials plus explicitly characterized native scaffolds.

    This is a fake-endpoint compatibility assertion, not semantic authorization
    for review_bridge_attempt or a production advisor route.
    """
    if (
        type(expected) is not ExpectedNativeRequest
        or expected.version != SUPPORTED_VERSION
        or not isinstance(expected.environment, str)
        or not 1 <= len(expected.environment) <= 2048
        or not isinstance(expected.prompt, str)
        or not 1 <= len(expected.prompt) <= MAX_REQUEST_BYTES
    ):
        _invalid()
    body = _strict_json(raw)
    if not isinstance(body, dict) or set(body) != {
        "model",
        "messages",
        "system",
        "tools",
        "metadata",
        "max_tokens",
        "thinking",
        "context_management",
        "output_config",
        "stream",
    }:
        _invalid()
    cached = {"type": "ephemeral"}
    messages = [
        {"role": "user", "content": expected.prompt},
        {
            "role": "system",
            "content": [{"type": "text", "text": expected.environment, "cache_control": cached}],
            "output_config": {"effort": "high"},
        },
    ]
    system = body["system"]
    if (
        body["model"] != MODEL
        or body["messages"] != messages
        or body["tools"] != []
        or type(body["max_tokens"]) is not int
        or body["max_tokens"] != 1024
        or body["thinking"] != {"type": "adaptive"}
        or body["context_management"]
        != {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
        or body["output_config"] != {"effort": "high"}
        or body["stream"] is not True
        or not isinstance(system, list)
        or len(system) != 3
        or not isinstance(system[0], dict)
        or set(system[0]) != {"type", "text"}
        or system[0]["type"] != "text"
        or not isinstance(system[0]["text"], str)
        or not re.fullmatch(
            r"x-anthropic-billing-header: cc_version="
            + re.escape(expected.version)
            + r"\.[0-9a-f]{3}; cc_entrypoint=sdk-cli;",
            system[0]["text"],
        )
        or system[1:]
        != [
            {"type": "text", "text": VENDOR_SYSTEM, "cache_control": cached},
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": cached},
        ]
    ):
        _invalid()
    metadata = body["metadata"]
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"user_id"}
        or not isinstance(metadata["user_id"], str)
        or len(metadata["user_id"]) > 256
    ):
        _invalid()
    user = _strict_json(metadata["user_id"].encode("utf-8"))
    if (
        not isinstance(user, dict)
        or set(user) != {"device_id", "account_uuid", "session_id"}
        or not isinstance(user["device_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", user["device_id"])
        or user["account_uuid"] != ""
        or user["session_id"] != NATIVE_SESSION_ID
    ):
        _invalid()
