"""Bounded diagnostic shape of a Claude stream event rejected by validation.

The shape names only known stream-json protocol vocabulary: event
type/subtype/state, content block types, boolean flags and top-level keys.
Any other name becomes ``?`` (keys are only counted), because an
identifier-shaped value can still be a tool name or a private identifier.
It never carries text, identifiers, paths or other values, so it may be kept
in the private job error detail to classify an unexpected native event.
"""

from __future__ import annotations

from collections.abc import Mapping

MAX_SHAPE_CHARACTERS = 400
_MAX_KEYS = 16
_MAX_BLOCK_TYPES = 6
_FLAGS = (("isMeta", "meta"), ("isReplay", "replay"), ("isSynthetic", "synthetic"))
_TYPES = frozenset(
    {
        "assistant",
        "attachment",
        "auth_status",
        "command_lifecycle",
        "control_cancel_request",
        "control_request",
        "control_response",
        "hook_progress",
        "hook_response",
        "hook_started",
        "keep_alive",
        "prompt_suggestion",
        "rate_limit_event",
        "result",
        "stream_event",
        "system",
        "task_notification",
        "task_progress",
        "task_started",
        "tool_progress",
        "tool_use_summary",
        "user",
    }
)
_SUBTYPES = frozenset(
    {
        "api_retry",
        "compact_boundary",
        "error_during_execution",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
        "error_max_turns",
        "hook_progress",
        "hook_response",
        "hook_started",
        "informational",
        "init",
        "local_command",
        "status",
        "success",
        "task_notification",
        "task_progress",
        "task_started",
        "turn_preempted",
    }
)
_STATES = frozenset({"cancelled", "completed", "dropped", "held", "queued", "released", "started"})
_BLOCK_TYPES = frozenset(
    {
        "document",
        "image",
        "mcp_tool_result",
        "mcp_tool_use",
        "redacted_thinking",
        "server_tool_use",
        "text",
        "thinking",
        "tool_result",
        "tool_use",
        "web_search_tool_result",
    }
)
_KEYS = frozenset(
    {
        "aborted",
        "agents",
        "apiKeySource",
        "cause",
        "claude_code_version",
        "command_uuid",
        "compact_metadata",
        "content",
        "cwd",
        "duration_api_ms",
        "duration_ms",
        "error",
        "event",
        "isMeta",
        "isReplay",
        "isSynthetic",
        "is_error",
        "mcp_servers",
        "message",
        "model",
        "modelUsage",
        "num_turns",
        "origin",
        "outcome",
        "output_style",
        "parent_tool_use_id",
        "permissionMode",
        "permission_denials",
        "plugins",
        "preempted_by_uuid",
        "priority",
        "queued_turn_count",
        "result",
        "session_id",
        "skills",
        "slash_commands",
        "state",
        "status",
        "stop_reason",
        "structured_output",
        "subtype",
        "terminal_reason",
        "timestamp",
        "tool_use_result",
        "tools",
        "total_cost_usd",
        "type",
        "usage",
        "user_message_uuids",
        "uuid",
    }
)


def _name(value: object, known: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in known else "?"


def _content(message: object) -> str:
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return "str"
    if not isinstance(content, list):
        return "other"
    kinds: list[str] = []
    for block in content:
        kind = _name(block.get("type") if isinstance(block, Mapping) else None, _BLOCK_TYPES)
        if kind not in kinds:
            kinds.append(kind)
    listed = "+".join(kinds[:_MAX_BLOCK_TYPES])
    return f"list:{listed}" + ("+..." if len(kinds) > _MAX_BLOCK_TYPES else "")


def claude_event_shape(event: Mapping[str, object]) -> str:
    kind = _name(event["type"], _TYPES) if "type" in event else "absent"
    parts = ["event", "type=" + kind]
    if "subtype" in event:
        parts.append("subtype=" + _name(event["subtype"], _SUBTYPES))
    if "state" in event:
        parts.append("state=" + _name(event["state"], _STATES))
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
    names = sorted(key for key in event if key in _KEYS)
    listed = names[:_MAX_KEYS]
    omitted = len(event) - len(listed)
    if listed or omitted:
        parts.append("keys=" + ",".join([*listed, *([f"+{omitted}"] if omitted else [])]))
    return " ".join(parts)[:MAX_SHAPE_CHARACTERS]
