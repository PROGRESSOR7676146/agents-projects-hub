from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .codex_accounts import CodexAccountStatus, CodexPoolStatus
from .codex_appserver import LimitWindow, RateLimits
from .provider_limits import ProviderLimit


def _name(value: str) -> str:
    if value == "gpt-5.6-sol":
        return "GPT-5.6 Sol"
    return value.replace("_", " ").replace("-", " ").title()


def _health(remaining: int | None, *, warning: int = 20) -> str:
    if remaining is None:
        return "🟡"
    if remaining <= 0:
        return "🔴"
    if remaining <= warning:
        return "🟡"
    return "🟢"


def _window(label: str, window: LimitWindow | None, timezone: ZoneInfo) -> str | None:
    if window is None:
        return None
    text = f"{_health(window.remaining_percent)} {label} {window.remaining_percent}%"
    if window.resets_at is not None:
        reset = datetime.fromtimestamp(window.resets_at, timezone)
        text += f" ↻ {reset:%d.%m %H:%M}"
    return text


def cached_codex_rate_limits(account: CodexAccountStatus | None) -> RateLimits:
    """Project a masked account snapshot into the normal compact status view."""
    if account is None:
        return RateLimits(None, None)
    primary = (
        LimitWindow(account.five_hour_remaining, account.five_hour_resets_at, None)
        if account.five_hour_remaining is not None
        else None
    )
    secondary = (
        LimitWindow(account.weekly_remaining, account.weekly_resets_at, None)
        if account.weekly_remaining is not None
        else None
    )
    return RateLimits(primary, secondary)


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
    provider_account_hints: dict[str, tuple[str, ...]] | None = None,
    provider_limits: dict[str, ProviderLimit] | None = None,
    timezone_name: str = "Europe/Moscow",
) -> str:
    lines: list[str] = []
    if pool.available:
        lines.append("Codex")
        for account in pool.accounts:
            remaining = [
                value
                for value in (account.five_hour_remaining, account.weekly_remaining)
                if value is not None
            ]
            unavailable = account.availability.casefold() not in {
                "available",
                "ready",
                "healthy",
                "unknown",
            }
            if unavailable or any(value <= 0 for value in remaining):
                marker = "🔴"
            elif account.quota_stale or any(value <= 20 for value in remaining):
                marker = "🟡"
            else:
                marker = "🟢"
            active = " ✓" if account.active else ""
            identity = account.identity_hint or f"account {account.index}"
            limits: list[str] = []
            if account.five_hour_remaining is not None:
                value = f"5h {account.five_hour_remaining}%"
                if account.five_hour_resets_at is not None:
                    reset = datetime.fromtimestamp(
                        account.five_hour_resets_at, ZoneInfo(timezone_name)
                    )
                    value += f" ↻ {reset:%d.%m %H:%M}"
                limits.append(value)
            if account.weekly_remaining is not None:
                value = f"week {account.weekly_remaining}%"
                if account.weekly_resets_at is not None:
                    reset = datetime.fromtimestamp(
                        account.weekly_resets_at, ZoneInfo(timezone_name)
                    )
                    value += f" ↻ {reset:%d.%m %H:%M}"
                limits.append(value)
            suffix = f" · {' · '.join(limits)}" if limits else ""
            lines.append(f"{marker}{active} {identity}{suffix}")
    if include_opencode_go:
        if lines:
            lines.append("")
        lines.extend(("OpenCode Go", "🟢 plan: 5h $12 · week $30 · month $60"))
        if opencode_limit is not None:
            reset = datetime.fromtimestamp(opencode_limit.resets_at, ZoneInfo(timezone_name))
            label = {
                "5-hour": "5h",
                "weekly": "Week",
                "monthly": "Month",
            }.get(opencode_limit.window, opencode_limit.window)
            lines.append(
                f"{_health(opencode_limit.remaining_percent)} {label} "
                f"{opencode_limit.remaining_percent}% ↻ {reset:%d.%m %H:%M}"
            )
    for provider, hints in (provider_account_hints or {}).items():
        if lines:
            lines.append("")
        lines.append(provider.replace("_", " ").replace("-", " ").title())
        lines.extend(f"🟡 {hint}… · limits unknown" for hint in hints)
        limit = (provider_limits or {}).get(provider)
        if limit is not None:
            reset = datetime.fromtimestamp(limit.resets_at, ZoneInfo(timezone_name))
            lines.append(
                f"{_health(limit.remaining_percent)} current account unknown · quota "
                f"{limit.remaining_percent}% ↻ {reset:%d.%m %H:%M}"
            )
    return "\n".join(lines)
