"""Bounded payload-free approval observations, without native approval authority."""

from __future__ import annotations

from .codex_activity import CodexActivityEvent, normalize_codex_approval_resolution

MAX_PENDING_ACTIVITY = 128
MAX_RESOLVED_ACTIVITY = 512
RequestKey = tuple[type, str | int]


class ActivityObservationUnavailable(RuntimeError):
    """Retire optional telemetry; never abort mandatory turn consumption."""


class CodexActivityRequests:
    def __init__(self) -> None:
        self.pending: dict[RequestKey, CodexActivityEvent] = {}
        self.resolved: dict[RequestKey, CodexActivityEvent] = {}

    def clear(self) -> None:
        self.pending.clear()
        self.resolved.clear()

    def request(self, event: CodexActivityEvent) -> bool:
        assert event.request_id is not None
        key = (type(event.request_id), event.request_id)
        prior = self.pending.get(key) or self.resolved.get(key)
        if prior is not None:
            if prior != event:
                raise ActivityObservationUnavailable("activity_request_identity_changed")
            return False
        if len(self.pending) >= MAX_PENDING_ACTIVITY:
            raise ActivityObservationUnavailable("activity_pending_requests_exhausted")
        self.pending[key] = event
        return True

    def resolve(self, message: dict) -> CodexActivityEvent | None:
        params = message.get("params")
        if not isinstance(params, dict):
            return None
        request_id = params.get("requestId")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return None
        key = (type(request_id), request_id)
        requested = self.pending.get(key)
        if requested is None:
            return None
        event = normalize_codex_approval_resolution(message, requested=requested)
        if event is None:
            return None
        # Never evict an old ID and later report its duplicate as a fresh wait.
        # Saturation retires this optional observer for the rest of the turn.
        if len(self.resolved) >= MAX_RESOLVED_ACTIVITY:
            raise ActivityObservationUnavailable("activity_resolved_requests_exhausted")
        del self.pending[key]
        self.resolved[key] = requested
        return event
