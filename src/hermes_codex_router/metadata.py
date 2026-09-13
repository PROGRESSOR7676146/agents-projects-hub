from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from .codex_appserver import LimitWindow, RateLimits, TurnResult

_ABSENT_DETAIL_VALUES = {"", "unavailable", "unknown", "none", "n/a"}
_MONTH_LABELS = (
    "",
    "Jan.",
    "Feb.",
    "Mar.",
    "Apr.",
    "May",
    "June",
    "July",
    "Aug.",
    "Sept.",
    "Oct.",
    "Nov.",
    "Dec.",
)


def _available_detail(value: str | None) -> bool:
    return value is not None and value.strip().casefold() not in _ABSENT_DETAIL_VALUES


def _compact_details(details: dict[str, str]) -> str:
    session = details.get("Session", "").strip()
    agent = details.get("Agent", "").strip()
    model = details.get("Model", "").strip()
    effort = details.get("Effort", "").strip()

    # Older callers included the agent in both fields.  Accept that shape while
    # keeping the user-facing footer free of duplicate information.
    if session and agent and session.endswith(f" · {agent}"):
        session = session[: -len(f" · {agent}")]

    identity = f"Session: {session}" if _available_detail(session) else ""
    if _available_detail(agent):
        identity += f" / Agent: {agent}" if identity else f"Agent: {agent}"
    if _available_detail(model):
        model_label = model
        if _available_detail(effort):
            model_label = f"{model_label}-{effort.casefold()}"
        identity += f" · {model_label}" if identity else model_label

    telemetry: list[str] = []
    context = details.get("Context remaining")
    if _available_detail(context):
        telemetry.append(f"Context remaining: {context}")

    for remaining_key, reset_key, label in (
        ("5-hour remaining", "5-hour reset", "5-hour remaining"),
        ("Weekly remaining", "Weekly reset", "Weekly remaining"),
    ):
        remaining = details.get(remaining_key)
        reset = details.get(reset_key)
        if not _available_detail(remaining) and not _available_detail(reset):
            continue
        value = remaining.strip() if _available_detail(remaining) and remaining else ""
        if _available_detail(reset) and reset:
            value = f"{value}, reset: {reset.strip()}" if value else f"reset: {reset.strip()}"
        telemetry.append(f"{label}: {value}")

    consumed = {
        "Session",
        "Agent",
        "Runtime",
        "Model",
        "Effort",
        "Context remaining",
        "5-hour remaining",
        "5-hour reset",
        "Weekly remaining",
        "Weekly reset",
    }
    for key, value in details.items():
        if key in consumed or not _available_detail(value):
            continue
        label = "Usage" if key == "Usage windows" else key
        telemetry.append(f"{label}: {value.strip()}")

    return "\n".join(part for part in (identity, *telemetry) if part)


def format_agent_response(answer: str, details: dict[str, str]) -> str:
    visible = html.escape(answer or "The agent completed without a visible text response.")
    block = _compact_details(details)
    if not block:
        return visible
    return f"{visible}\n\n<blockquote expandable>{html.escape(block)}</blockquote>"


def _reset_text(window: LimitWindow | None, timezone: ZoneInfo) -> str:
    if window is None or window.resets_at is None:
        return "unavailable"
    reset = datetime.fromtimestamp(window.resets_at, timezone)
    return f"{_MONTH_LABELS[reset.month]} {reset.day}, {reset:%H:%M}"


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
