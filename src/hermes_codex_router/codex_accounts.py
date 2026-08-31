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
_EMAIL = re.compile(r"([A-Za-z0-9][A-Za-z0-9.*_-]*)@([A-Za-z0-9.*_-]+(?:\.[A-Za-z0-9_-]+)+)")


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


def read_codex_pool_status(
    root: Path,
    *,
    executable: str = "codex-multi-auth",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    identity_hints: dict[int, str] | None = None,
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
                    identity_hint=(
                        f"{identity_hints[index + 1]}…"
                        if identity_hints and index + 1 in identity_hints
                        else _masked_identity_hint(label)
                    ),
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
