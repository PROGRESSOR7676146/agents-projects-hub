"""Bounded byte-only input for the native Claude user-message protocol.

Signatures attest the content class, not decoded validity or model comprehension.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Sequence

MAX_CLAUDE_IMAGE_BYTES = 2 * 1024 * 1024
MAX_CLAUDE_IMAGE_AGGREGATE_BYTES = 4 * 1024 * 1024
MAX_CLAUDE_INPUT_BYTES = 8 * 1024 * 1024
CLAUDE_IMAGE_OVERHEAD_RESERVE = 64 * 1024
MAX_CLAUDE_IMAGES = 10


class ClaudeImageInputError(ValueError):
    def __init__(self) -> None:
        super().__init__("Claude verified image input is invalid or exceeds its byte limits.")


@dataclass(frozen=True, slots=True)
class VerifiedClaudeImage:
    position: int
    media_type: str
    data: bytes = field(repr=False)
    sha256: str


def image_signature_matches(data: bytes, media_type: str) -> bool:
    return (media_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or (
        media_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")
    )


def encode_claude_image_input(
    prompt: str, images: Sequence[VerifiedClaudeImage], session_id: str
) -> bytes:
    """Serialize one new user frame. Never accepts a path, URL or JSON source."""
    try:
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > MAX_CLAUDE_INPUT_BYTES
            or not isinstance(session_id, str)
            or str(uuid.UUID(session_id)) != session_id
            or not 1 <= len(images) <= MAX_CLAUDE_IMAGES
        ):
            raise ClaudeImageInputError()
        raw_prompt_size = len(prompt.encode("utf-8"))
        escaped_size = raw_prompt_size + prompt.count('"') + prompt.count("\\")
        for character in range(32):
            escaped_size += prompt.count(chr(character)) * (
                1 if character in (8, 9, 10, 12, 13) else 5
            )
        if escaped_size > MAX_CLAUDE_INPUT_BYTES:
            raise ClaudeImageInputError()
        total = 0
        previous = 0
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for image in images:
            if (
                not isinstance(image, VerifiedClaudeImage)
                or type(image.position) is not int
                or not previous < image.position <= MAX_CLAUDE_IMAGES
                or not isinstance(image.data, bytes)
                or not 0 < len(image.data) <= MAX_CLAUDE_IMAGE_BYTES
                or not image_signature_matches(image.data, image.media_type)
                or hashlib.sha256(image.data).hexdigest() != image.sha256
            ):
                raise ClaudeImageInputError()
            previous = image.position
            total += len(image.data)
            if total > MAX_CLAUDE_IMAGE_AGGREGATE_BYTES:
                raise ClaudeImageInputError()
            content.extend(
                (
                    {"type": "text", "text": f"MATERIAL {image.position} IMAGE"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": image.media_type, "data": ""},
                    },
                )
            )
        frame = {
            "type": "user",
            "session_id": session_id,
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()),
        }
        skeleton = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        expanded = sum(4 * ((len(image.data) + 2) // 3) for image in images)
        if len(skeleton) + expanded + 1 > MAX_CLAUDE_INPUT_BYTES - CLAUDE_IMAGE_OVERHEAD_RESERVE:
            raise ClaudeImageInputError()
        for index, image in enumerate(images):
            source = content[index * 2 + 2]["source"]
            assert isinstance(source, dict)
            source["data"] = base64.b64encode(image.data).decode("ascii")
        encoded = (
            json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        if len(encoded) > MAX_CLAUDE_INPUT_BYTES - CLAUDE_IMAGE_OVERHEAD_RESERVE:
            raise ClaudeImageInputError()
        return encoded
    except (TypeError, UnicodeError, ValueError, AttributeError):
        raise ClaudeImageInputError() from None
