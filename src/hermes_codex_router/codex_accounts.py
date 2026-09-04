from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

_ID_SUFFIX = re.compile(r"\[id:([^\]]+)\]")
_EMAIL = re.compile(r"([A-Za-z0-9][A-Za-z0-9.*_-]*)@([A-Za-z0-9.*_-]+(?:\.[A-Za-z0-9_-]+)+)")
_LIVE_QUOTA = re.compile(
    r"5h\s+(\d+)% left \(resets ([^)]+)\),\s*"
    r"7d\s+(\d+)% left \(resets ([^)]+)\)"
)
_AUTH_INVALIDATION_MARKER = "token-invalid"
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        1,
    )
}


@dataclass(frozen=True, slots=True)
class CodexAccountStatus:
    index: int
    active: bool
    availability: str
    risk: str
    five_hour_remaining: int | None
    weekly_remaining: int | None
    five_hour_resets_at: int | None
    weekly_resets_at: int | None
    quota_updated_at: int | None
    quota_stale: bool
    identity_hint: str | None = None
    auth_invalidated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.index,
            "active": self.active,
            "availability": self.availability,
            "risk": self.risk,
            "five_hour_remaining_percent": self.five_hour_remaining,
            "weekly_remaining_percent": self.weekly_remaining,
            "five_hour_resets_at": self.five_hour_resets_at,
            "weekly_resets_at": self.weekly_resets_at,
            "quota_updated_at": self.quota_updated_at,
            "quota_stale": self.quota_stale,
            "identity_hint": self.identity_hint,
            "auth_invalidated": self.auth_invalidated,
        }


@dataclass(frozen=True, slots=True)
class CodexPoolStatus:
    available: bool
    rotation_enabled: bool
    accounts: tuple[CodexAccountStatus, ...]
    recommended_account: int | None
    account_rotations: int
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "rotation_enabled": self.rotation_enabled,
            "recommended_account": self.recommended_account,
            "account_rotations": self.account_rotations,
            "accounts": [account.as_dict() for account in self.accounts],
            "error": self.error,
        }


def encode_codex_pool_snapshot(status: CodexPoolStatus) -> str:
    """Serialize only bounded, masked account telemetry for the Controller."""
    value = {
        "v": 1,
        "ok": status.available,
        "rotation": status.rotation_enabled,
        "recommended": status.recommended_account,
        "rotations": status.account_rotations,
        "error": status.error,
        "accounts": [
            {
                "i": item.index,
                "active": item.active,
                "availability": item.availability,
                "risk": item.risk,
                "5h": item.five_hour_remaining,
                "week": item.weekly_remaining,
                "5h_reset": item.five_hour_resets_at,
                "week_reset": item.weekly_resets_at,
                "updated": item.quota_updated_at,
                "stale": item.quota_stale,
                "hint": item.identity_hint,
                "auth": item.auth_invalidated,
            }
            for item in status.accounts
        ],
    }
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    if len(encoded) > 1000:
        raise ValueError("Codex pool snapshot exceeds runtime-event bound")
    return encoded


def decode_codex_pool_snapshot(value: str) -> CodexPoolStatus:
    try:
        raw = json.loads(value)
        if not isinstance(raw, dict) or raw.get("v") != 1:
            raise ValueError("unsupported Codex pool snapshot")
        raw_accounts = raw.get("accounts")
        if not isinstance(raw_accounts, list):
            raise ValueError("invalid Codex pool snapshot accounts")
        accounts = tuple(
            CodexAccountStatus(
                index=int(item["i"]),
                active=item.get("active") is True,
                availability=str(item["availability"])[:64],
                risk=str(item["risk"])[:64],
                five_hour_remaining=_integer(item.get("5h")),
                weekly_remaining=_integer(item.get("week")),
                five_hour_resets_at=_integer(item.get("5h_reset")),
                weekly_resets_at=_integer(item.get("week_reset")),
                quota_updated_at=_integer(item.get("updated")),
                quota_stale=item.get("stale") is True,
                identity_hint=(str(item["hint"])[:32] if item.get("hint") else None),
                auth_invalidated=item.get("auth") is True,
            )
            for item in raw_accounts
            if isinstance(item, dict)
        )
        return CodexPoolStatus(
            available=raw.get("ok") is True,
            rotation_enabled=raw.get("rotation") is True,
            accounts=accounts,
            recommended_account=_integer(raw.get("recommended")),
            account_rotations=_integer(raw.get("rotations")) or 0,
            error=str(raw["error"])[:128] if raw.get("error") else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Codex pool snapshot") from exc


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _remaining(window: dict[str, Any]) -> int | None:
    used = window.get("usedPercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return max(0, min(100, round(100 - used)))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _masked_identity_hint(label: str) -> str | None:
    match = _EMAIL.search(label)
    if match is None:
        return None
    local, domain = match.groups()
    visible = local.split("*", 1)[0][:4] or local[:2]
    if "*" not in local:
        visible = local[:2]
    suffix = domain.rsplit(".", 1)[-1]
    return f"{visible}***@***.{suffix}"


def _live_reset_at(value: str, *, observed_at: datetime, timezone_name: str) -> int | None:
    local = observed_at.astimezone(ZoneInfo(timezone_name))
    match = re.fullmatch(r"(\d{2}):(\d{2})(?: on ([A-Z][a-z]{2}) (\d{2}))?", value)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    month_name, day_text = match.group(3), match.group(4)
    try:
        if month_name is None or day_text is None:
            candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate < local - timedelta(minutes=5):
                candidate += timedelta(days=1)
        else:
            candidate = local.replace(
                month=_MONTHS[month_name],
                day=int(day_text),
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if candidate < local - timedelta(days=1):
                candidate = candidate.replace(year=candidate.year + 1)
    except (KeyError, ValueError):
        return None
    return int(candidate.timestamp())


def _live_quota_values(
    row: dict[str, Any], *, observed_at: datetime, timezone_name: str
) -> tuple[int, int, int | None, int | None] | None:
    live = _object(row.get("liveQuota"))
    summary = live.get("summary")
    if not isinstance(summary, str) or (match := _LIVE_QUOTA.search(summary)) is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(3)),
        _live_reset_at(match.group(2), observed_at=observed_at, timezone_name=timezone_name),
        _live_reset_at(match.group(4), observed_at=observed_at, timezone_name=timezone_name),
    )


def read_codex_pool_status(
    root: Path,
    *,
    executable: str = "codex-multi-auth",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    identity_hints: dict[int, str] | None = None,
    live: bool = False,
    timezone_name: str = "UTC",
) -> CodexPoolStatus:
    """Read a redacted pool snapshot; credentials never enter this process."""
    try:
        argv = (executable, "auth", "report", "--json", *(("--live",) if live else ()))
        try:
            completed = runner(
                argv, check=True, capture_output=True, text=True, timeout=30 if live else 10
            )
        except (OSError, subprocess.SubprocessError):
            if not live:
                raise
            live = False
            completed = runner(
                (executable, "auth", "report", "--json"),
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        report = json.loads(completed.stdout)
        if not isinstance(report, dict):
            raise ValueError("multi-auth report is not an object")
        quota = _read_json(root / "quota-cache.json")
        settings = _read_json(root / "settings.json")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        return CodexPoolStatus(False, False, (), None, 0, type(exc).__name__)

    quota_by_id = _object(quota.get("byAccountId"))
    forecast = _object(report.get("forecast"))
    recommendation = _object(forecast.get("recommendation"))
    rows = forecast.get("accounts")
    selected_is_explicit = isinstance(rows, list) and any(
        _object(item).get("selected") is True for item in rows
    )
    generated_at = report.get("generatedAt")
    try:
        observed_at = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        observed_at = datetime.now().astimezone()
    accounts: list[CodexAccountStatus] = []
    if isinstance(rows, list):
        for position, raw_row in enumerate(rows):
            row = _object(raw_row)
            index = _integer(row.get("index"))
            if index is None:
                index = position
            raw_label = row.get("label")
            label: str = raw_label if isinstance(raw_label, str) else ""
            raw_availability = row.get("availability")
            availability: str = raw_availability if isinstance(raw_availability, str) else "unknown"
            raw_risk = row.get("riskLevel")
            risk: str = raw_risk if isinstance(raw_risk, str) else "unknown"
            raw_reasons = row.get("reasons")
            auth_invalidated = isinstance(raw_reasons, list) and any(
                isinstance(reason, str) and _AUTH_INVALIDATION_MARKER in reason.casefold()
                for reason in raw_reasons
            )
            suffix_match = _ID_SUFFIX.search(label)
            account_quota: dict[str, Any] = {}
            if suffix_match:
                suffix = suffix_match.group(1)
                matches = [
                    value
                    for account_id, value in quota_by_id.items()
                    if isinstance(account_id, str)
                    and account_id.endswith(suffix)
                    and isinstance(value, dict)
                ]
                if len(matches) == 1:
                    account_quota = matches[0]
            primary = _object(account_quota.get("primary"))
            secondary = _object(account_quota.get("secondary"))
            updated_ms = _integer(account_quota.get("updatedAt"))
            updated_at = updated_ms // 1000 if updated_ms is not None else None
            live_values = (
                _live_quota_values(row, observed_at=observed_at, timezone_name=timezone_name)
                if live
                else None
            )
            if live_values is not None:
                five_hour_remaining, weekly_remaining, five_hour_reset, weekly_reset = live_values
                updated_at = int(observed_at.timestamp())
            else:
                five_hour_remaining, weekly_remaining = _remaining(primary), _remaining(secondary)
                five_hour_reset = (
                    value // 1000
                    if (value := _integer(primary.get("resetAtMs"))) is not None
                    else None
                )
                weekly_reset = (
                    value // 1000
                    if (value := _integer(secondary.get("resetAtMs"))) is not None
                    else None
                )
            accounts.append(
                CodexAccountStatus(
                    index=index + 1,
                    active=(
                        row.get("selected") is True
                        if selected_is_explicit
                        else row.get("isCurrent") is True
                    ),
                    availability=availability,
                    risk=risk,
                    five_hour_remaining=five_hour_remaining,
                    weekly_remaining=weekly_remaining,
                    five_hour_resets_at=five_hour_reset,
                    weekly_resets_at=weekly_reset,
                    quota_updated_at=updated_at,
                    quota_stale=(
                        live_values is None
                        and (updated_at is None or time.time() - updated_at > 30 * 60)
                    ),
                    identity_hint=(
                        f"{identity_hints[index + 1]}…"
                        if identity_hints and index + 1 in identity_hints
                        else _masked_identity_hint(label)
                    ),
                    auth_invalidated=auth_invalidated,
                )
            )

    plugin = _object(settings.get("pluginConfig"))
    runtime = _object(report.get("runtime"))
    metrics = _object(runtime.get("runtimeMetrics"))
    recommended = _integer(recommendation.get("recommendedIndex"))
    return CodexPoolStatus(
        available=True,
        rotation_enabled=plugin.get("codexRuntimeRotationProxy") is True,
        accounts=tuple(accounts),
        recommended_account=recommended + 1 if recommended is not None else None,
        account_rotations=_integer(metrics.get("accountRotations")) or 0,
    )


def format_codex_pool_status(status: CodexPoolStatus, *, timezone_name: str) -> str:
    if not status.available:
        return f"Codex pool: unavailable ({status.error or 'unknown error'})"
    lines = [
        f"Codex rotation: {'enabled' if status.rotation_enabled else 'disabled'}",
        f"Codex accounts: {len(status.accounts)}; recommended: "
        f"{status.recommended_account or 'unknown'}; rotations: {status.account_rotations}",
    ]
    timezone = ZoneInfo(timezone_name)
    for account in status.accounts:
        marker = "active" if account.active else account.availability
        identity = f" ({account.identity_hint})" if account.identity_hint else ""
        limits = []
        if account.five_hour_remaining is not None:
            limits.append(f"5h {account.five_hour_remaining}%")
        if account.weekly_remaining is not None:
            limits.append(f"week {account.weekly_remaining}%")
        updated = ""
        if account.quota_updated_at is not None:
            timestamp = datetime.fromtimestamp(account.quota_updated_at, timezone)
            updated = f"; checked {timestamp:%d.%m %H:%M}"
            if account.quota_stale:
                updated += " (stale)"
        quota_text = ", ".join(limits) if limits else "quota unknown"
        lines.append(f"Account {account.index}{identity}: {marker}; {quota_text}{updated}")
    return "\n".join(lines)
