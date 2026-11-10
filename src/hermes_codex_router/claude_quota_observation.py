"""Bounded, non-identifying projections of observed Claude quota events."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from .diagnostic_log import survived

QuotaObservationCallback = Callable[[dict[str, object]], None]
TerminalFailureCode = Callable[[dict[str, object], str | None], str]
MAX_QUOTA_OBSERVATIONS = 8
_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})
_LIMIT_TYPES = frozenset(
    {
        "five_hour",
        "seven_day",
        "seven_day_sonnet",
        "seven_day_opus",
        "seven_day_oauth_apps",
        "seven_day_cowork",
        "overage",
        "iguana_necktie",
    }
)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)
_MAX_EPOCH = 253402300799


def _reset_time(value: object) -> str | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not 0 <= value <= _MAX_EPOCH:
                return None
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str) and len(value) <= 40 and _TIMESTAMP.fullmatch(value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            if not 0 <= parsed.timestamp() <= _MAX_EPOCH:
                return None
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def quota_observation(event: dict[str, object], *, quota_result: bool) -> dict[str, object] | None:
    """Project only fixed vocabulary and a validated reset, never provider text."""
    kind = event.get("type")
    if (
        kind not in ("rate_limit_event", "quota_event")
        and not (kind == "assistant" and event.get("error") == "rate_limit")
        and not (kind == "result" and quota_result)
    ):
        return None
    nested = event.get("rate_limit_info")
    fields = nested if isinstance(nested, dict) else {}

    def field(name: str) -> object:
        return fields[name] if name in fields else event.get(name)

    status = field("status")
    limit_type = field("rateLimitType")
    api_status = event.get("api_error_status")
    if kind == "result" and type(api_status) is int and api_status == 429:
        status = 429
    elif not (
        (type(status) is int and status == 429) or (isinstance(status, str) and status in _STATUSES)
    ):
        status = None
    projection: dict[str, object] = {
        "event_type": kind,
        "status": status,
        "rateLimitType": (
            limit_type if isinstance(limit_type, str) and limit_type in _LIMIT_TYPES else None
        ),
        "reset_present": "resetsAt" in fields or "resetsAt" in event,
    }
    if (reset := _reset_time(field("resetsAt"))) is not None:
        projection["reset_at"] = reset
    return projection


def _priority(observation: dict[str, object]) -> int:
    if observation["event_type"] == "result":
        return 3
    status = observation["status"]
    if status in ("allowed_warning", "rejected", 429) or observation["event_type"] == "assistant":
        return 2
    return 0 if status == "allowed" else 1


class QuotaObserver:
    """Keep bounded, distinct evidence and publish it after stream validation."""

    def __init__(self, callback: QuotaObservationCallback, failure_code: TerminalFailureCode):
        self._callback = callback
        self._failure_code = failure_code
        self._observations: list[dict[str, object]] = []
        self._last: dict[str, object] | None = None
        self._assistant_error: str | None = None

    @property
    def has_observations(self) -> bool:
        return bool(self._observations)

    def observe(self, event: dict[str, object], session_id: str | None) -> None:
        kind = event.get("type")
        if kind == "assistant":
            self._assistant_error = (
                "rate_limit"
                if session_id is not None
                and event.get("session_id") == session_id
                and event.get("error") == "rate_limit"
                else None
            )
        api_status = event.get("api_error_status")
        quota_result = (
            kind == "result"
            and event.get("is_error") is True
            and (
                (type(api_status) is int and api_status == 429)
                or self._failure_code(event, self._assistant_error) == "claude_quota_exhausted"
            )
        )
        if kind == "result":
            self._assistant_error = None
        observation = quota_observation(event, quota_result=quota_result)
        if observation is None or observation == self._last:
            return
        self._last = observation
        if len(self._observations) == MAX_QUOTA_OBSERVATIONS:
            # Protect warnings and rejections from less informative events, and
            # terminal results from everything else. Evict the oldest lowest priority.
            lowest = min(map(_priority, self._observations))
            if _priority(observation) < lowest:
                return
            index = next(
                i for i, item in enumerate(self._observations) if _priority(item) == lowest
            )
            self._observations.pop(index)
        self._observations.append(observation)

    def publish(self, *, stream_validated: bool) -> None:
        observations, self._observations = self._observations, []
        for observation in observations:
            try:
                self._callback({**observation, "stream_validated": stream_validated})
            except Exception as error:
                survived("claude_stream.quota_observation", error)
