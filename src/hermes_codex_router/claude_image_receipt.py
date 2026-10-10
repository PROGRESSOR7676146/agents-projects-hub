"""Current processed-image receipt, separate from file-tool permission policy."""

from __future__ import annotations

import base64
import json
import re
import uuid

from .claude_image_input import (
    MAX_CLAUDE_IMAGE_AGGREGATE_BYTES,
    MAX_CLAUDE_IMAGE_BYTES,
    MAX_CLAUDE_IMAGES,
    MAX_CLAUDE_INPUT_BYTES,
    ClaudeImageInputError,
    image_signature_matches,
)
from .claude_stream import ClaudeStreamError


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _image_size(block: object) -> int:
    """Validate an inline processed source, without asserting pixel equivalence."""
    if not isinstance(block, dict) or set(block) != {"type", "source"} or block["type"] != "image":
        raise ValueError()
    source = block["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"type", "media_type", "data"}
        or source["type"] != "base64"
        or source["media_type"] not in ("image/png", "image/jpeg")
        or not isinstance(source["data"], str)
        or len(source["data"]) > 4 * ((MAX_CLAUDE_IMAGE_BYTES + 2) // 3)
    ):
        raise ValueError()
    data = base64.b64decode(source["data"], validate=True)
    if (
        not 0 < len(data) <= MAX_CLAUDE_IMAGE_BYTES
        or not image_signature_matches(data, source["media_type"])
        or base64.b64encode(data).decode("ascii") != source["data"]
    ):
        raise ValueError()
    return len(data)


class ClaudeImageReceipt:
    """Observe one caller-UUID acknowledgement; never retain or publish its data."""

    def __init__(self, input_data: bytes, *, expected_session_id: str | None) -> None:
        try:
            if len(input_data) > MAX_CLAUDE_INPUT_BYTES:
                raise ClaudeImageInputError()
            frame = json.loads(input_data)
            if (
                not isinstance(frame, dict)
                or set(frame) != {"type", "message", "session_id", "parent_tool_use_id", "uuid"}
                or frame["type"] != "user"
                or frame["parent_tool_use_id"] is not None
                or frame["session_id"] != expected_session_id
                or not _uuid(frame["session_id"])
                or not _uuid(frame["uuid"])
                or not isinstance(frame["message"], dict)
                or set(frame["message"]) != {"role", "content"}
                or frame["message"]["role"] != "user"
                or not isinstance(frame["message"]["content"], list)
            ):
                raise ClaudeImageInputError()
            self.session_id = frame["session_id"]
            self.message_id = frame["uuid"]
            self._content = frame["message"]["content"]
            if (
                not 3 <= len(self._content) <= 1 + 2 * MAX_CLAUDE_IMAGES
                or len(self._content) % 2 != 1
                or not isinstance(self._content[0], dict)
                or set(self._content[0]) != {"type", "text"}
                or self._content[0].get("type") != "text"
                or not isinstance(self._content[0].get("text"), str)
                or not self._content[0]["text"].strip()
            ):
                raise ClaudeImageInputError()
            total = previous = 0
            for index in range(1, len(self._content), 2):
                marker = self._content[index]
                if (
                    not isinstance(marker, dict)
                    or set(marker) != {"type", "text"}
                    or marker["type"] != "text"
                ):
                    raise ClaudeImageInputError()
                match = re.fullmatch(r"MATERIAL ([1-9]|10) IMAGE", marker["text"])
                if match is None or int(match[1]) <= previous:
                    raise ClaudeImageInputError()
                previous = int(match[1])
                total += _image_size(self._content[index + 1])
                if total > MAX_CLAUDE_IMAGE_AGGREGATE_BYTES:
                    raise ClaudeImageInputError()
            self._extensions = [
                "png" if item["source"]["media_type"] == "image/png" else "jpg"
                for item in self._content
                if isinstance(item, dict) and item.get("type") == "image"
            ]
            if not self._extensions:
                raise ClaudeImageInputError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ClaudeImageInputError() from None
        self._acknowledged = False
        self._lifecycle: list[str] = []
        self._terminal = False
        self._success = False

    def observe(self, event: dict[str, object]) -> bool:
        """Return whether to retain the event after the tools-disabled guard."""
        kind = event.get("type")
        if kind == "command_lifecycle":
            state = event.get("state")
            if (
                set(event) != {"type", "state", "session_id", "command_uuid", "uuid"}
                or event.get("session_id") != self.session_id
                or event.get("command_uuid") != self.message_id
                or not _uuid(event.get("uuid"))
                or self._lifecycle != ["queued", "started", "completed"][: len(self._lifecycle)]
                or len(self._lifecycle) >= 3
                or state != ["queued", "started", "completed"][len(self._lifecycle)]
                or self._terminal
                and state != "completed"
            ):
                raise ClaudeStreamError("claude image input lifecycle is unverified")
            assert isinstance(state, str)
            self._lifecycle.append(state)
            return False
        if kind == "user":
            message = event.get("message")
            if (
                self._terminal
                or self._acknowledged
                or not set(event).issubset(
                    {
                        "type",
                        "message",
                        "session_id",
                        "parent_tool_use_id",
                        "uuid",
                        "isReplay",
                        "timestamp",
                    }
                )
                or event.get("session_id") != self.session_id
                or event.get("uuid") != self.message_id
                or event.get("isReplay") is not True
                or "parent_tool_use_id" not in event
                or event["parent_tool_use_id"] is not None
                or not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message.get("role") != "user"
                or not isinstance(message.get("content"), list)
            ):
                raise ClaudeStreamError("claude image input acknowledgement is unverified")
            content = message["content"]
            if len(content) < len(self._content):
                raise ClaudeStreamError("claude image input was replaced or omitted")
            total = 0
            for original, processed in zip(self._content, content):
                if original["type"] == "text":
                    if original != processed:
                        raise ClaudeStreamError("claude image input was replaced or omitted")
                    continue
                try:
                    total += _image_size(processed)
                    # The pinned CLI characterizes unchanged inputs, native
                    # JPEG re-encoding and PNG-to-JPEG processing only.
                    if (
                        total > MAX_CLAUDE_IMAGE_AGGREGATE_BYTES
                        or original["source"]["media_type"] == "image/jpeg"
                        and processed["source"]["media_type"] != "image/jpeg"
                    ):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise ClaudeStreamError("claude image input was replaced or omitted") from None
            tail = content[len(self._content) :]
            if len(tail) > len(self._extensions):
                raise ClaudeStreamError("claude image input annotation is unverified")
            for block, extension in zip(tail, self._extensions):
                pattern = (
                    r"\[Image: source: /tmp/claude-[0-9]+/-[A-Za-z0-9_.-]+/"
                    + re.escape(self.session_id)
                    + r"/images/[0-9]+\."
                    + extension
                    + r"\]"
                )
                if (
                    not isinstance(block, dict)
                    or set(block) != {"type", "text"}
                    or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                    or re.fullmatch(pattern, block["text"]) is None
                ):
                    raise ClaudeStreamError("claude image input annotation is unverified")
            self._acknowledged = True
            return False
        if kind == "result":
            self._terminal = True
            self._success = event.get("subtype") == "success" and event.get("is_error") is False
        return True

    def finish(self) -> None:
        if self._success and not self._acknowledged:
            raise ClaudeStreamError("claude image input acknowledgement is missing")
