"""Strict fictional payload and exact-current-turn evidence checks."""

from __future__ import annotations

import re

from tests.codex_native_mcp_server import PROBE_KEYS, SERVER, decode_json, valid_nonce


class McpEvidenceError(RuntimeError):
    pass


def _payload(value: object, nonce: str) -> dict:
    if (
        not valid_nonce(nonce)
        or not isinstance(value, dict)
        or set(value) != {"version", "nonce", "direct", "child"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["nonce"] != nonce
    ):
        raise McpEvidenceError("invalid_mcp_payload")
    for name in ("direct", "child"):
        probe = value[name]
        if not isinstance(probe, dict) or set(probe) != PROBE_KEYS:
            raise McpEvidenceError("invalid_mcp_probe_shape")
        if not all(type(field) is bool for field in probe.values()):
            raise McpEvidenceError("invalid_mcp_probe_types")
    return value


def _result(result: object, nonce: str) -> dict:
    if not isinstance(result, dict):
        raise McpEvidenceError("missing_mcp_result")
    content = result.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
        or not isinstance(content[0].get("text"), str)
    ):
        raise McpEvidenceError("invalid_mcp_content")
    try:
        textual = _payload(decode_json(content[0]["text"]), nonce)
    except (ValueError, TypeError) as error:
        raise McpEvidenceError("invalid_mcp_text_json") from error
    structured = _payload(result.get("structuredContent"), nonce)
    if textual != structured:
        raise McpEvidenceError("conflicting_mcp_payloads")
    return structured


def direct_mcp_probe(result: object, nonce: str, before: int, after: int) -> dict:
    if not isinstance(result, dict):
        raise McpEvidenceError("invalid_direct_mcp_result")
    if type(before) is not int or type(after) is not int or before < 0 or after != before:
        raise McpEvidenceError("direct_diagnostic_used_responses")
    if result.get("isError") is not None and result.get("isError") is not False:
        raise McpEvidenceError("direct_mcp_call_failed")
    return _result(result, nonce)


def proven_mcp_probe(result: object, nonce: str) -> dict:
    if not isinstance(result, dict):
        raise McpEvidenceError("invalid_mcp_turn_result")
    thread_id, turn_id = result.get("thread_id"), result.get("turn_id")
    if (
        result.get("status") != "completed"
        or not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(turn_id, str)
        or not turn_id
    ):
        raise McpEvidenceError("mcp_current_turn_not_completed")
    items = _items(result, thread_id, turn_id, "mcpToolCall")
    if len(items) != 1:
        raise McpEvidenceError("mcp_current_item_not_unique")
    item = items[0]
    if (
        item.get("status") != "completed"
        or item.get("server") != SERVER
        or item.get("tool") != "probe"
        or item.get("arguments") != {"nonce": nonce}
        or item.get("error") is not None
    ):
        raise McpEvidenceError("mcp_current_item_not_successful")
    return _result(item.get("result"), nonce)


def _items(result: dict, thread_id: str, turn_id: str, kind: str) -> list[dict]:
    rows = result.get("items")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and isinstance(row.get("item"), dict) for row in rows
    ):
        raise McpEvidenceError("invalid_mcp_evidence_rows")
    return [
        row["item"]
        for row in rows
        if row.get("thread_id") == thread_id
        and row.get("turn_id") == turn_id
        and row["item"].get("type") == kind
    ]


def proven_command_custody(result: dict) -> dict[str, bool]:
    thread_id, turn_id = result.get("thread_id"), result.get("turn_id")
    if (
        result.get("status") != "completed"
        or not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(turn_id, str)
        or not turn_id
    ):
        raise McpEvidenceError("custody_command_turn_not_completed")
    items = _items(result, thread_id, turn_id, "commandExecution")
    if len(items) != 1:
        raise McpEvidenceError("custody_command_not_unique")
    item = items[0]
    if (
        item.get("status") != "completed"
        or type(item.get("exitCode")) is not int
        or item["exitCode"] != 0
    ):
        raise McpEvidenceError("custody_command_not_successful")
    output = item.get("aggregatedOutput", "")
    matches = re.findall(r"EXAMPLE_CUSTODY:(\{[^\n]*\})", output)
    if output.count("EXAMPLE_CUSTODY:") != 1 or len(matches) != 1:
        raise McpEvidenceError("custody_command_output_not_unique")
    try:
        probe = decode_json(matches[0])
    except ValueError as error:
        raise McpEvidenceError("custody_command_invalid_json") from error
    if not isinstance(probe, dict) or set(probe) != PROBE_KEYS:
        raise McpEvidenceError("custody_command_invalid_shape")
    if not all(type(field) is bool for field in probe.values()):
        raise McpEvidenceError("custody_command_invalid_types")
    return probe
