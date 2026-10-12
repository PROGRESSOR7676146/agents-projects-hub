"""Dependency-neutral bounded values and UTC timestamps for state domains."""

from __future__ import annotations

from datetime import datetime, timezone

from .state_errors import StateError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise StateError("timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat()


def _bounded(value: str, *, name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise StateError(f"invalid {name}")
    return normalized


def _optional_bounded(value: str | None, *, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded(value, name=name, maximum=maximum)
