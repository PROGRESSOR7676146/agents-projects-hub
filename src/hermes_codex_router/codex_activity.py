"""Pure, payload-free activity observations for one already identified Codex turn."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

CodexActivityKind = Literal[
    "tool_started",
    "tool_completed",
    "tool_output",
    "visible_message_completed",
    "retrying",
    "approval_requested",
    "approval_resolved",
]
CodexActivityCategory = Literal[
    "command",
    "file_change",
    "mcp",
    "dynamic_tool",
    "collaboration",
    "web_search",
    "image_view",
    "visible_message",
    "retry",
    "network",
    "permissions",
]
CodexVisiblePhase = Literal["commentary", "final_answer", "unknown"]


@dataclass(frozen=True, slots=True)
class CodexActivityEvent:
    kind: CodexActivityKind
    category: CodexActivityCategory
    thread_id: str
    turn_id: str
    item_id: str | None = None
    request_id: str | int | None = None
    phase: CodexVisiblePhase | None = None


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", re.ASCII)
_TOOLS: dict[str, tuple[CodexActivityCategory, frozenset[str] | None]] = {
    "commandExecution": ("command", frozenset({"completed", "failed", "declined"})),
    "fileChange": ("file_change", frozenset({"completed", "failed", "declined"})),
    "mcpToolCall": ("mcp", frozenset({"completed", "failed"})),
    "dynamicToolCall": ("dynamic_tool", frozenset({"completed", "failed"})),
    "collabAgentToolCall": ("collaboration", frozenset({"completed", "failed"})),
    # These documented item types carry no required status field.
    "webSearch": ("web_search", None),
    "imageView": ("image_view", None),
}
_APPROVALS: dict[str, CodexActivityCategory] = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
}


def _identity(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def _request_identity(value: object) -> str | int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if -(2**63) <= value < 2**63 else None
    return _identity(value)


def normalize_codex_approval_resolution(
    message: object, *, requested: CodexActivityEvent
) -> CodexActivityEvent | None:
    """Map a resolution to earlier exact approval evidence, never infer its turn.

    The native resolution has a thread and request ID, but no required turn ID.
    Resolving a request is observation only, not proof of allowance or terminality.
    """
    if requested.kind != "approval_requested" or not isinstance(message, dict):
        return None
    if (
        message.get("method") != "serverRequest/resolved"
        or any(key in message for key in ("id", "result", "error"))
        or message.get("jsonrpc", "2.0") != "2.0"
    ):
        return None
    params = message.get("params")
    if not isinstance(params, dict) or params.get("threadId") != requested.thread_id:
        return None
    request_id = _request_identity(params.get("requestId"))
    if type(request_id) is not type(requested.request_id) or request_id != requested.request_id:
        return None
    if "turnId" in params and params["turnId"] != requested.turn_id:
        return None
    return replace(requested, kind="approval_resolved")


def normalize_codex_activity(
    message: object, *, expected_thread_id: str, expected_turn_id: str
) -> CodexActivityEvent | None:
    """Allow only scoped activity, never infer IDs or retain provider payloads.

    Unknown or malformed messages return ``None``. The caller supplies exact
    identities established elsewhere; this observation has no execution,
    terminality, approval, or replay authority. Approval request IDs preserve
    their JSON integer/string distinction.
    """
    thread_id, turn_id = _identity(expected_thread_id), _identity(expected_turn_id)
    if thread_id is None or turn_id is None or not isinstance(message, dict):
        return None
    if "result" in message or "error" in message or message.get("jsonrpc", "2.0") != "2.0":
        return None
    method, params = message.get("method"), message.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return None
    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
        return None
    if method in _APPROVALS:
        request_id = _request_identity(message.get("id"))
        item_id = _identity(params.get("itemId"))
        if request_id is None or item_id is None:
            return None
        category = _APPROVALS[method]
        if category == "permissions" and not isinstance(params.get("permissions"), dict):
            return None
        network = params.get("networkApprovalContext")
        if category == "command" and network is not None:
            if not isinstance(network, dict) or not all(
                isinstance(network.get(key), str) and network[key].strip()
                for key in ("host", "protocol")
            ):
                return None
            category = "network"
        return CodexActivityEvent(
            "approval_requested",
            category,
            thread_id,
            turn_id,
            item_id,
            request_id,
        )
    if "id" in message:
        return None
    if method == "error":
        if params.get("willRetry") is True and isinstance(params.get("error"), dict):
            return CodexActivityEvent("retrying", "retry", thread_id, turn_id)
        return None
    if method == "item/commandExecution/outputDelta":
        item_id, delta = _identity(params.get("itemId")), params.get("delta")
        if item_id is not None and isinstance(delta, str) and delta.strip():
            return CodexActivityEvent("tool_output", "command", thread_id, turn_id, item_id)
        return None
    if method not in {"item/started", "item/completed"}:
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_id, item_type = _identity(item.get("id")), item.get("type")
    if item_id is None or not isinstance(item_type, str):
        return None
    if "itemId" in params and params["itemId"] != item_id:
        return None
    if item_type == "agentMessage":
        text, phase = item.get("text"), item.get("phase")
        if method != "item/completed" or not isinstance(text, str) or not text.strip():
            return None
        if phase is None:
            visible_phase: CodexVisiblePhase = "unknown"
        elif phase in ("commentary", "final_answer"):
            visible_phase = phase
        else:
            return None
        return CodexActivityEvent(
            "visible_message_completed",
            "visible_message",
            thread_id,
            turn_id,
            item_id,
            phase=visible_phase,
        )
    tool = _TOOLS.get(item_type)
    if tool is None:
        return None
    category, completed_statuses = tool
    status = item.get("status")
    if completed_statuses is not None or "status" in item:
        valid = {"inProgress"} if method == "item/started" else completed_statuses or {"completed"}
        if not isinstance(status, str) or status not in valid:
            return None
    kind: CodexActivityKind = "tool_started" if method == "item/started" else "tool_completed"
    return CodexActivityEvent(kind, category, thread_id, turn_id, item_id)
