from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

_ID_SUFFIX = re.compile(r"\[id:([^\]]+)\]")


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


def read_codex_pool_status(
    root: Path,
    *,
    executable: str = "codex-multi-auth",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CodexPoolStatus:
    """Read a redacted pool snapshot; credentials never enter this process."""
    try:
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
            accounts.append(
                CodexAccountStatus(
                    index=index + 1,
                    active=row.get("isCurrent") is True,
                    availability=availability,
                    risk=risk,
                    five_hour_remaining=_remaining(primary),
                    weekly_remaining=_remaining(secondary),
                    five_hour_resets_at=(
                        value // 1000
                        if (value := _integer(primary.get("resetAtMs"))) is not None
                        else None
                    ),
                    weekly_resets_at=(
                        value // 1000
                        if (value := _integer(secondary.get("resetAtMs"))) is not None
                        else None
                    ),
                    quota_updated_at=updated_at,
                    quota_stale=updated_at is None or time.time() - updated_at > 30 * 60,
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
        lines.append(f"Account {account.index}: {marker}; {quota_text}{updated}")
    return "\n".join(lines)
