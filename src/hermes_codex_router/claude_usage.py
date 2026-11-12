"""Passive per-request Claude usage from one turn's stream-json events.

Each native API request is one assistant ``message.id``; the CLI repeats its
usage on every content block, so the latest snapshot per id wins and nothing
is summed twice. The result's cumulative totals are never used for context.
Only fixed numeric fields are kept: no text, tools, prompts or provider errors.
"""

from __future__ import annotations

import json
from collections.abc import Callable

USAGE_METADATA_KEY = "claude_usage_v1"
LARGE_CONTEXT_TOKENS = 150_000
# A cache write of at least 50k tokens covering most of a request's context means
# the cached prefix was rebuilt (expiry, prefix change or compaction); the cause
# is not known.
_LARGE_WRITE_SHARE = 0.5
_LARGE_WRITE_MIN_TOKENS = 50_000
_MAX_TRACKED_REQUESTS = 10_000
_MAX_TOKENS = 100_000_000

UsageSummaryCallback = Callable[[dict[str, object]], None]


def _tokens(usage: dict[str, object], name: str) -> int | None:
    value = usage.get(name)
    if type(value) is int and 0 <= value <= _MAX_TOKENS:
        return value
    return None


def _snapshot(usage: object) -> dict[str, int | None]:
    if not isinstance(usage, dict):
        usage = {}
    creation = usage.get("cache_creation")
    split = creation if isinstance(creation, dict) else {}
    return {
        "input": _tokens(usage, "input_tokens"),
        "cache_read": _tokens(usage, "cache_read_input_tokens"),
        "cache_write": _tokens(usage, "cache_creation_input_tokens"),
        "cache_write_5m": _tokens(split, "ephemeral_5m_input_tokens"),
        "cache_write_1h": _tokens(split, "ephemeral_1h_input_tokens"),
        "output": _tokens(usage, "output_tokens"),
    }


def _context(snapshot: dict[str, int | None]) -> int | None:
    parts = (snapshot["input"], snapshot["cache_read"], snapshot["cache_write"])
    return None if any(part is None for part in parts) else sum(parts)  # type: ignore[arg-type]


def _total(snapshots: list[dict[str, int | None]], name: str) -> int | None:
    values = [snapshot[name] for snapshot in snapshots]
    return None if not values or any(value is None for value in values) else sum(values)  # type: ignore[arg-type]


class ClaudeUsageObserver:
    """Collect request usage while the stream drains; summarize once at the end."""

    def __init__(self) -> None:
        self._main: dict[str, dict[str, int | None]] = {}
        self._subagent: set[str] = set()
        self._context_after_last_request = True
        self._untracked = False

    def observe(self, event: dict[str, object]) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "compact_boundary":
            # Compaction replaces the context; until another request reports
            # usage, the current size is unknown.
            self._context_after_last_request = False
            return
        if kind != "assistant":
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if not isinstance(message_id, str) or not 0 < len(message_id) <= 200:
            return
        if event.get("parent_tool_use_id") is not None:
            if len(self._subagent) < _MAX_TRACKED_REQUESTS:
                self._subagent.add(message_id)
            return
        # A request without usage still counts: its fields stay unknown, so it
        # cannot leave an older request looking like the latest one.
        snapshot = _snapshot(message.get("usage"))
        if message_id not in self._main and len(self._main) >= _MAX_TRACKED_REQUESTS:
            self._untracked = True
            return
        # Re-inserting keeps stream order: the latest request is the last key.
        self._main.pop(message_id, None)
        self._main[message_id] = snapshot
        self._context_after_last_request = True

    def summary(self) -> dict[str, object] | None:
        if not self._main:
            return None
        snapshots = list(self._main.values())
        contexts = [_context(snapshot) for snapshot in snapshots]
        known = [value for value in contexts if value is not None]
        large_writes = sum(
            1
            for snapshot, context in zip(snapshots, contexts)
            if context is not None
            and snapshot["cache_write"] is not None
            and snapshot["cache_write"] >= _LARGE_WRITE_MIN_TOKENS
            and snapshot["cache_write"] > context * _LARGE_WRITE_SHARE
        )
        return {
            "requests": len(snapshots),
            "subagent_requests": len(self._subagent),
            "last_context": (
                contexts[-1] if self._context_after_last_request and not self._untracked else None
            ),
            "max_context": max(known) if known else None,
            "cache_write": _total(snapshots, "cache_write"),
            "cache_write_5m": _total(snapshots, "cache_write_5m"),
            "cache_write_1h": _total(snapshots, "cache_write_1h"),
            "cache_read": _total(snapshots, "cache_read"),
            "output": _total(snapshots, "output"),
            "large_cache_writes": large_writes,
        }


def usage_metadata_json(summary: dict[str, object] | None) -> str | None:
    """Return the bounded result metadata that carries one turn's usage summary."""
    if summary is None:
        return None
    return json.dumps({USAGE_METADATA_KEY: summary}, sort_keys=True, separators=(",", ":"))


def usage_from_metadata(metadata_json: str | None) -> dict[str, object] | None:
    if not metadata_json:
        return None
    try:
        value = json.loads(metadata_json)
    except json.JSONDecodeError:
        return None
    summary = value.get(USAGE_METADATA_KEY) if isinstance(value, dict) else None
    return summary if isinstance(summary, dict) else None


def format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{round(value / 1_000)}k"
    return str(value)


def last_context_tokens(summary: dict[str, object] | None) -> int | None:
    value = summary.get("last_context") if summary else None
    return value if type(value) is int else None


def usage_footer_details(summary: dict[str, object] | None) -> dict[str, str]:
    """Footer fields for one Claude turn; absent evidence stays unavailable."""
    context = last_context_tokens(summary)
    details = {
        "Context": (
            f"{format_tokens(context)} (last request)" if context is not None else "unavailable"
        )
    }
    large_writes = summary.get("large_cache_writes") if summary else None
    if type(large_writes) is int and large_writes > 0:
        details["Large cache writes"] = str(large_writes)
    if context is not None and context >= LARGE_CONTEXT_TOKENS:
        details["Notice"] = (
            "Large context can increase request cost. Consider /new for the next task."
        )
    return details


__all__ = [
    "LARGE_CONTEXT_TOKENS",
    "USAGE_METADATA_KEY",
    "ClaudeUsageObserver",
    "UsageSummaryCallback",
    "format_tokens",
    "last_context_tokens",
    "usage_footer_details",
    "usage_from_metadata",
    "usage_metadata_json",
]
