from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

_LIMIT = re.compile(
    r"(?P<window>5-hour|weekly|monthly) usage limit reached\.\s*Resets in "
    r"(?P<duration>(?:\d+\s*(?:d|day|days|hr|hour|hours|min|minute|minutes)\s*)+)",
    re.IGNORECASE,
)
_PART = re.compile(r"(\d+)\s*(d|day|days|hr|hour|hours|min|minute|minutes)", re.I)


@dataclass(frozen=True, slots=True)
class ProviderLimit:
    provider: str
    window: str
    remaining_percent: int
    resets_at: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def parse_opencode_limit(text: str, *, now: datetime | None = None) -> ProviderLimit | None:
    match = _LIMIT.search(text)
    if match is None or "429" not in text:
        return None
    duration = timedelta()
    for amount, unit in _PART.findall(match.group("duration")):
        value = int(amount)
        if unit.casefold().startswith("d"):
            duration += timedelta(days=value)
        elif unit.casefold().startswith("h"):
            duration += timedelta(hours=value)
        else:
            duration += timedelta(minutes=value)
    if duration <= timedelta():
        return None
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return ProviderLimit(
        provider="opencode-go",
        window=match.group("window").casefold(),
        remaining_percent=0,
        resets_at=round((observed + duration).timestamp()),
    )


def decode_provider_limit(detail: str) -> ProviderLimit | None:
    try:
        value = json.loads(detail)
        return ProviderLimit(
            provider=str(value["provider"]),
            window=str(value["window"]),
            remaining_percent=int(value["remaining_percent"]),
            resets_at=int(value["resets_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
