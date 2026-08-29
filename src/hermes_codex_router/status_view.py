from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .codex_accounts import CodexPoolStatus
from .codex_appserver import LimitWindow, RateLimits
from .provider_limits import ProviderLimit


def _name(value: str) -> str:
    if value == "gpt-5.6-sol":
        return "GPT-5.6 Sol"
    return value.replace("_", " ").replace("-", " ").title()


def _window(label: str, window: LimitWindow | None, timezone: ZoneInfo) -> str | None:
    if window is None:
        return None
    text = f"{label} {window.remaining_percent}%"
    if window.resets_at is not None:
        reset = datetime.fromtimestamp(window.resets_at, timezone)
        text += f" ↻ {reset:%d.%m %H:%M}"
    return text


def format_session_status(
    *,
    agent: str,
    model: str,
    effort: str,
    writer: str,
    context_remaining: float | None,
    account_hint: str | None,
    limits: RateLimits,
    timezone_name: str,
) -> str:
    headline = f"{agent} · {_name(model)} · {effort.title()}"
    if writer != "telegram":
        headline += f" · {writer.title()}"
    detail: list[str] = []
    if context_remaining is not None:
        detail.append(f"Context {context_remaining:.1f}%")
    if account_hint:
        detail.append(f"Account {account_hint}")
    lines = [headline]
    if detail:
        lines.append(" · ".join(detail))
    timezone = ZoneInfo(timezone_name)
    windows = [
        value
        for value in (
            _window("5h", limits.primary, timezone),
            _window("Week", limits.secondary, timezone),
        )
        if value is not None
    ]
    lines.extend(windows)
    return "\n".join(lines)


def format_accounts(
    pool: CodexPoolStatus,
    *,
    include_opencode_go: bool,
    opencode_limit: ProviderLimit | None = None,
    timezone_name: str = "Europe/Moscow",
) -> str:
    lines: list[str] = []
    if pool.available:
        lines.append("Codex")
        for account in pool.accounts:
            marker = "✓" if account.active else "•"
            identity = account.identity_hint or f"account {account.index}"
            limits: list[str] = []
            if account.five_hour_remaining is not None:
                limits.append(f"5h {account.five_hour_remaining}%")
            if account.weekly_remaining is not None:
                limits.append(f"week {account.weekly_remaining}%")
            suffix = f" · {' · '.join(limits)}" if limits else ""
            lines.append(f"{marker} {identity}{suffix}")
    if include_opencode_go:
        if lines:
            lines.append("")
        lines.extend(("OpenCode Go", "✓ plan: 5h $12 · week $30 · month $60"))
        if opencode_limit is not None:
            reset = datetime.fromtimestamp(opencode_limit.resets_at, ZoneInfo(timezone_name))
            label = {
                "5-hour": "5h",
                "weekly": "Week",
                "monthly": "Month",
            }.get(opencode_limit.window, opencode_limit.window)
            lines.append(f"{label} {opencode_limit.remaining_percent}% ↻ {reset:%d.%m %H:%M}")
    return "\n".join(lines)
