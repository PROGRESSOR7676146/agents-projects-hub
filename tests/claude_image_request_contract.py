"""Pinned fictional images and exact restored history for native CLI proof only."""

from __future__ import annotations

import base64
import json
from typing import Any

from tests.claude_native_request_contract import (
    NATIVE_SESSION_ID,
    PROMPT,
    ExpectedNativeRequest,
    NativeRequestContractError,
    _strict_json,
    validate_request_body,
)

# Valid synthetic 2x2 images; no image library or operator file is needed.
IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGMUsUlhYGBgYmBgYGBgAAAISgC4OkO5"
    "PAAAAABJRU5ErkJggg==",
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIs"
    "IxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
    "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAk"
    "M2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QA"
    "HwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
    "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
    "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDzSiiivYPLP//Z",
)
IMAGE_BYTES = tuple(base64.b64decode(value, validate=True) for value in IMAGE_BASE64)
MARKERS = ("example-image-1", "example-image-2")
MISSING_SESSION_ID = "019abcde-1234-7fff-8fff-0123456789ab"


def validate_missing_session_result(raw: bytes, *, returncode: int) -> None:
    """Require one explicit refusal, never count a parser exception as proof."""
    from hermes_codex_router.claude_stream import (
        ClaudeStreamError,
        ClaudeTerminalFailure,
        parse_claude_stream,
    )

    body = _strict_json(raw)
    if (
        type(returncode) is not int
        or not 0 <= returncode <= 255
        or not isinstance(body, dict)
        or body.get("type") != "result"
        or body.get("subtype") != "error_during_execution"
        or body.get("is_error") is not True
        or body.get("session_id") != MISSING_SESSION_ID
        or body.get("errors") != ["No conversation found with session ID: " + MISSING_SESSION_ID]
        or body.get("result") not in (None, "")
        or body.get("api_error_status") is not None
    ):
        raise NativeRequestContractError("native_missing_session_refusal_unproven")
    try:
        parse_claude_stream(
            raw.decode("utf-8"), expected_session_id=MISSING_SESSION_ID, returncode=returncode
        )
    except ClaudeTerminalFailure as error:
        if error.code == "claude_provider_failure" and error.session_id == MISSING_SESSION_ID:
            return
    except ClaudeStreamError:
        pass
    raise NativeRequestContractError("native_missing_session_refusal_unproven")


def selected_content(phase: int) -> list[dict[str, Any]]:
    if type(phase) is not int or phase not in (0, 1):
        raise NativeRequestContractError("native_image_phase_invalid")
    return [
        {"type": "text", "text": "Example " + ("PNG" if phase == 0 else "JPEG") + " image."},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png" if phase == 0 else "image/jpeg",
                "data": IMAGE_BASE64[phase],
            },
        },
    ]


def input_message(phase: int) -> bytes:
    """Only new material: no prior history, native path or approval grant."""
    return (
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": selected_content(phase)},
                "parent_tool_use_id": None,
                "session_id": NATIVE_SESSION_ID,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def expected_messages(expected: ExpectedNativeRequest, phase: int, *, uid: int) -> list[dict]:
    if (
        type(expected) is not ExpectedNativeRequest
        or expected.version != "2.1.285"
        or type(uid) is not int
        or not 0 <= uid <= 2**32 - 1
    ):
        raise NativeRequestContractError("native_image_contract_invalid")

    def user(index: int) -> dict:
        extension = "png" if index == 0 else "jpg"
        # This native-added scaffold is fixed inside the fictional namespace.
        # It is never supplied on stdin or treated as authority to read a path.
        annotation = (
            "[Image: source: /tmp/claude-"
            + str(uid)
            + "/-workspace-example/"
            + NATIVE_SESSION_ID
            + "/images/"
            + str(index + 1)
            + "."
            + extension
            + "]"
        )
        return {
            "role": "user",
            "content": selected_content(index) + [{"type": "text", "text": annotation}],
        }

    selected_content(phase)  # Validate before constructing history.
    system = {
        "role": "system",
        "content": [
            {"type": "text", "text": expected.environment, "cache_control": {"type": "ephemeral"}}
        ],
    }
    if phase == 0:
        return [user(0), {**system, "output_config": {"effort": "high"}}]
    return [
        user(0),
        {"role": "system", "content": expected.environment, "output_config": {"effort": "high"}},
        {"role": "assistant", "content": [{"type": "text", "text": MARKERS[0]}]},
        user(1),
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "<total_tokens>15000000 tokens left</total_tokens>",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
    ]


def validate_image_request(
    raw: bytes, expected: ExpectedNativeRequest, phase: int, *, uid: int
) -> None:
    body = _strict_json(raw)
    messages = expected_messages(expected, phase, uid=uid)
    if not isinstance(body, dict) or body.get("messages") != messages:
        raise NativeRequestContractError("native_image_request_invalid")
    for index, message in enumerate(m for m in body["messages"] if m["role"] == "user"):
        source = message["content"][1]["source"]
        if base64.b64decode(source["data"], validate=True) != IMAGE_BYTES[index]:
            raise NativeRequestContractError("native_image_bytes_invalid")
    # Exact message history was independently checked above. Retain the existing
    # full native model/system/tools/metadata/header compatibility contract.
    body["messages"] = [
        {"role": "user", "content": PROMPT},
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": expected.environment,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "output_config": {"effort": "high"},
        },
    ]
    validate_request_body(json.dumps(body).encode("utf-8"), expected)
