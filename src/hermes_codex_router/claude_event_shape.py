"""Bounded diagnostic shape of a Claude stream event rejected by validation.

The shape names protocol fields only: event type/subtype/state, content block
types, boolean flags and top-level key names. It never carries text, tool
names, identifiers, paths or other values, so it may be kept in the private
job error detail to classify an unexpected native event after the fact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

MAX_SHAPE_CHARACTERS = 400
_MAX_KEYS = 16
_MAX_BLOCK_TYPES = 6
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_FLAGS = (("isMeta", "meta"), ("isReplay", "replay"), ("isSynthetic", "synthetic"))


def _name(value: object) -> str:
    return value if isinstance(value, str) and _NAME.fullmatch(value) else "?"


def _content(message: object) -> str:
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return "str"
    if not isinstance(content, list):
        return "other"
    kinds: list[str] = []
    for block in content:
        kind = _name(block.get("type") if isinstance(block, Mapping) else None)
        if kind not in kinds:
            kinds.append(kind)
    listed = "+".join(kinds[:_MAX_BLOCK_TYPES])
    return f"list:{listed}" + ("+..." if len(kinds) > _MAX_BLOCK_TYPES else "")


def claude_event_shape(event: Mapping[str, object]) -> str:
    parts = ["event", "type=" + (_name(event["type"]) if "type" in event else "absent")]
    for field in ("subtype", "state"):
        if field in event:
            parts.append(f"{field}={_name(event[field])}")
    if "message" in event:
        parts.append("content=" + _content(event["message"]))
    for field, label in _FLAGS:
        if field in event:
            value = event[field]
            parts.append(
                f"{label}={'true' if value is True else 'false' if value is False else 'other'}"
            )
    if "parent_tool_use_id" in event:
        parts.append("parent=" + ("null" if event["parent_tool_use_id"] is None else "set"))
    names = sorted(key for key in event if _NAME.fullmatch(key))
    if names:
        listed = names[:_MAX_KEYS]
        omitted = len(event) - len(listed)
        parts.append("keys=" + ",".join(listed) + (f",+{omitted}" if omitted else ""))
    return " ".join(parts)[:MAX_SHAPE_CHARACTERS]
