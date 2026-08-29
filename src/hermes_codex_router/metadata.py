from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from .codex_appserver import LimitWindow, RateLimits, TurnResult


def format_agent_response(answer: str, details: dict[str, str]) -> str:
    visible = html.escape(answer or "The agent completed without a visible text response.")
    block = "\n".join(f"{key}: {value}" for key, value in details.items())
    return f"{visible}\n\n<blockquote expandable>{html.escape(block)}</blockquote>"


def _reset_text(window: LimitWindow | None, timezone: ZoneInfo) -> str:
    if window is None or window.resets_at is None:
        return "unavailable"
    return datetime.fromtimestamp(window.resets_at, timezone).strftime("%Y-%m-%d %H:%M %Z")


def _remaining_text(window: LimitWindow | None) -> str:
    if window is None:
        return "unavailable"
    return f"{window.remaining_percent}%"


def _context_remaining(result: TurnResult) -> str:
    if not result.context_window or result.context_tokens_used is None:
        return "unavailable"
    remaining = max(0, result.context_window - result.context_tokens_used)
    return f"{remaining * 100 / result.context_window:.1f}%"


def format_telegram_response(
    *,
    result: TurnResult,
    agent: str,
    model: str,
    effort: str,
    session_label: str,
    limits: RateLimits,
    timezone_name: str,
) -> str:
    timezone = ZoneInfo(timezone_name)
    details = "\n".join(
        [
            f"Session: {session_label}",
            f"Agent: {agent}",
            f"Model: {model}",
            f"Effort: {effort}",
            f"Context remaining: {_context_remaining(result)}",
            f"5-hour remaining: {_remaining_text(limits.primary)}",
            f"5-hour reset: {_reset_text(limits.primary, timezone)}",
            f"Weekly remaining: {_remaining_text(limits.secondary)}",
            f"Weekly reset: {_reset_text(limits.secondary, timezone)}",
        ]
    )
    return format_agent_response(
        result.text or "Codex completed the turn without a text response.",
        dict(line.split(": ", 1) for line in details.splitlines()),
    )
