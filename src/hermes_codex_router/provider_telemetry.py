from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .hub_config import ProviderTelemetrySettings


@dataclass(frozen=True, slots=True)
class ProviderTelemetry:
    account_hint: str | None
    model: str | None
    effort: str | None
    context_remaining: float | None
    quota_remaining: int | None
    quota_resets_at: int | None
    stale: bool


@dataclass(frozen=True, slots=True)
class ProviderTelemetryHealth:
    ok: bool
    detail: str


def _read_private_json(path: Path) -> dict[str, object]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
        or path.stat().st_size > 131072
    ):
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _source_health(path: Path, *, now: float, max_age_seconds: int) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return "missing-or-symlink"
        metadata = path.stat()
        if metadata.st_mode & 0o077:
            return "unsafe-permissions"
        if metadata.st_size > 131072:
            return "oversize"
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "invalid-json"
    except OSError:
        return "unreadable"
    if not isinstance(value, dict):
        return "invalid-json"
    timestamp = value.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return "missing-timestamp"
    return "stale" if now - float(timestamp) > max_age_seconds else "fresh"


def probe_antigravity_telemetry(
    settings: ProviderTelemetrySettings,
    *,
    max_age_seconds: int = 900,
    now: float | None = None,
) -> ProviderTelemetryHealth:
    current = time.time() if now is None else now
    quota = _source_health(
        settings.quota_cache,
        now=current,
        max_age_seconds=max_age_seconds,
    )
    status = _source_health(
        settings.status_state,
        now=current,
        max_age_seconds=max_age_seconds,
    )
    return ProviderTelemetryHealth(
        ok=quota == "fresh" and status == "fresh",
        detail=f"quota={quota}, status={status}",
    )


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _account_hint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    local = value.split("@", 1)[0]
    safe = "".join(character for character in local if character.isalnum())
    return f"{safe[:3]}…" if len(safe) >= 3 else None


def load_antigravity_telemetry(
    settings: ProviderTelemetrySettings,
    *,
    selected_model: str,
    selected_effort: str,
    max_age_seconds: int = 900,
    now: float | None = None,
) -> ProviderTelemetry:
    current = time.time() if now is None else now
    quota = _read_private_json(settings.quota_cache)
    status = _read_private_json(settings.status_state)
    quota_timestamp = quota.get("timestamp")
    quota_stale = not isinstance(quota_timestamp, (int, float)) or (
        current - float(quota_timestamp) > max_age_seconds
    )
    scope = quota.get("scope")
    hint = _account_hint(scope.get("email")) if isinstance(scope, dict) else None

    raw_status_model = status.get("model")
    status_model: str | None = raw_status_model if isinstance(raw_status_model, str) else None
    effective_model = (
        status_model if selected_model == "provider-selected" and status_model else selected_model
    )
    requested = _normalize(f"{effective_model} {selected_effort}")
    models = quota.get("models")
    entry: dict[str, object] | None = None
    if isinstance(models, dict):
        exact = models.get(requested)
        if isinstance(exact, dict):
            entry = exact
        else:
            for key, value in models.items():
                normalized = _normalize(str(key))
                if (
                    isinstance(value, dict)
                    and normalized
                    and (normalized in requested or requested in normalized)
                ):
                    entry = value
                    break
    remaining = entry.get("remaining_percentage") if entry else None
    quota_remaining = (
        max(0, min(100, round(float(remaining))))
        if isinstance(remaining, (int, float)) and not quota_stale
        else None
    )
    reset_epoch: int | None = None
    reset_value = entry.get("reset_time") if entry else None
    if isinstance(reset_value, str) and not quota_stale:
        try:
            reset_epoch = int(
                datetime.fromisoformat(reset_value.replace("Z", "+00:00")).timestamp()
            )
        except ValueError:
            pass

    status_timestamp = status.get("timestamp")
    status_fresh = isinstance(status_timestamp, (int, float)) and (
        current - float(status_timestamp) <= max_age_seconds
    )
    status_matches = bool(
        status_fresh
        and status_model
        and (
            selected_model == "provider-selected"
            or _normalize(f"{selected_model} {selected_effort}") == _normalize(status_model)
        )
    )
    context = status.get("context_remaining_percentage") if status_matches else None
    context_remaining = (
        max(0.0, min(100.0, float(context))) if isinstance(context, (int, float)) else None
    )
    effort_match = re.search(r"\((low|medium|high|max)\)\s*$", status_model or "", re.I)
    return ProviderTelemetry(
        account_hint=hint,
        model=status_model if status_matches else None,
        effort=effort_match.group(1).lower() if effort_match else None,
        context_remaining=context_remaining,
        quota_remaining=quota_remaining,
        quota_resets_at=reset_epoch,
        stale=quota_stale,
    )
