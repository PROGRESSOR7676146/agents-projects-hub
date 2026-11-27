"""Recognize the native coordinate note following a resized Read image."""

from __future__ import annotations

from collections.abc import Collection
from decimal import ROUND_HALF_UP, Decimal

from .claude_input_lifecycle import is_event_uuid
from .claude_stream import ClaudeStreamError


class ClaudeReadImage:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._reads: set[str] = set()
        self._notes: set[str] = set()
        self._next_note: str | None = None

    def observe(
        self, event: dict[str, object], *, input_ids: Collection[str], acknowledged: bool
    ) -> bool:
        expected, self._next_note = self._next_note, None
        kind = event.get("type")
        if kind == "user" and event.get("isSynthetic") is True:
            identity = event.get("uuid")
            if (
                not acknowledged
                or expected is None
                or event.get("session_id") != self.session_id
                or "parent_tool_use_id" not in event
                or event["parent_tool_use_id"] is not None
                or not isinstance(identity, str)
                or not is_event_uuid(identity)
                or identity in input_ids
                or identity in self._notes
                or set(event)
                - {
                    "type",
                    "session_id",
                    "parent_tool_use_id",
                    "uuid",
                    "timestamp",
                    "isSynthetic",
                    "message",
                }
                or event.get("message")
                != {
                    "role": "user",
                    "content": [{"type": "text", "text": expected}],
                }
            ):
                raise ClaudeStreamError("claude synthetic user event is unverified")
            self._notes.add(identity)
            return True
        if not acknowledged or event.get("session_id") != self.session_id:
            return False
        if "parent_tool_use_id" not in event or event["parent_tool_use_id"] is not None:
            return False
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != kind:
            return False
        content = message.get("content")
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Read"
                    and isinstance(identity := block.get("id"), str)
                    and 0 < len(identity) <= 128
                ):
                    self._reads.add(identity)
        if kind == "user" and "isReplay" not in event and isinstance(content, list):
            self._next_note = self._read_result(content, event.get("tool_use_result"))
        return False

    def _read_result(self, content: list[object], output: object) -> str | None:
        if len(content) != 1 or not isinstance(block := content[0], dict):
            return None
        identity = block.get("tool_use_id")
        if (
            block.get("type") != "tool_result"
            or not isinstance(identity, str)
            or identity not in self._reads
        ):
            return None
        self._reads.remove(identity)
        image = block.get("content")
        if (
            block.get("is_error", False) is not False
            or not isinstance(image, list)
            or len(image) != 1
            or not isinstance(image[0], dict)
            or image[0].get("type") != "image"
            or not isinstance(output, dict)
            or output.get("type") != "image"
        ):
            return None
        file = output.get("file")
        dimensions = file.get("dimensions") if isinstance(file, dict) else None
        if not isinstance(dimensions, dict):
            return None
        sizes: list[int] = []
        for key in ("originalWidth", "originalHeight", "displayWidth", "displayHeight"):
            size = dimensions.get(key)
            if type(size) is not int or not 0 < size <= 1_000_000:
                return None
            sizes.append(size)
        width, height, shown_width, shown_height = sizes
        if (width, height) == (shown_width, shown_height):
            return None
        # Native Number.toFixed rounds a binary midpoint away from zero.
        scale = Decimal(width / shown_width).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return (
            f"[Image: original {width}x{height}, displayed at {shown_width}x{shown_height}. "
            f"Multiply coordinates by {scale} to map to original image.]"
        )
