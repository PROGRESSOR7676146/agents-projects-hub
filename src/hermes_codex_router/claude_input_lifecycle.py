"""Correlate native lifecycle records with a submitted input and its result."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from .claude_stream import ClaudeStreamError


def is_event_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class ClaudeInputLifecycle:
    def __init__(self, session_id: str, initial_uuid: str, input_kind: str) -> None:
        self.session_id = session_id
        self.initial_uuid = initial_uuid
        self.input_kind = input_kind
        self.history: dict[str, list[str]] = {}
        self.event_ids: set[str] = set()
        self.cancelled = False
        self._activity_seen = False
        self._denial_id: str | None = None

    def observe_command(
        self,
        event: dict[str, object],
        *,
        input_ids: Collection[str],
        acknowledged: bool,
        terminal: bool,
    ) -> None:
        command, identity, state = (event.get(k) for k in ("command_uuid", "uuid", "state"))
        if (
            set(event) != {"type", "state", "session_id", "command_uuid", "uuid"}
            or event.get("session_id") != self.session_id
            or not isinstance(state, str)
            or not isinstance(command, str)
            or command not in input_ids
            or not isinstance(identity, str)
            or not is_event_uuid(identity)
            or identity in input_ids
            or identity in self.event_ids
            or identity == self._denial_id
        ):
            raise self._unverified()
        history = self.history.setdefault(command, [])
        if state == "cancelled":
            if (
                len(input_ids) != 1
                or not acknowledged
                or history != ["queued", "started"]
                or self._activity_seen
                or (terminal and self._denial_id is None)
            ):
                raise self._unverified()
            self.cancelled = True
        elif (
            len(history) >= 3
            or state != ("queued", "started", "completed")[len(history)]
            or (terminal and state != "completed")
        ):
            raise self._unverified()
        history.append(state)
        self.event_ids.add(identity)

    def observe_activity(self, event: dict[str, object]) -> None:
        kind = event.get("type")
        if kind == "assistant" and self._bound_api_error(event):
            message = event.get("message")
            if (
                event.get("is_api_error_message") is True
                and isinstance(message, dict)
                and message.get("model") == "<synthetic>"
                and isinstance(message.get("content"), list)
                and all(
                    isinstance(block, dict) and block.get("type") == "text"
                    for block in message["content"]
                )
            ):
                return
        self._activity_seen = True
        if self.cancelled:
            raise self._unverified()

    def observe_result(self, event: dict[str, object]) -> None:
        identity = event.get("uuid")
        self._denial_id = (
            identity
            if isinstance(identity, str)
            and is_event_uuid(identity)
            and identity != self.initial_uuid
            and identity not in self.event_ids
            and self._bound_api_error(event)
            and event.get("is_error") is True
            and event.get("terminal_reason") == "api_error"
            else None
        )
        self.finish()

    def _bound_api_error(self, event: dict[str, object]) -> bool:
        return (
            event.get("session_id") == self.session_id
            and event.get("parent_tool_use_id") is None
            and event.get("user_message_uuid") == self.initial_uuid
            and event.get("user_message_uuids") == [self.initial_uuid]
            and type(event.get("api_error_status")) is int
            and event.get("api_error_status") == 403
            and event.get("api_error_type") == "permission_error"
        )

    def finish(self) -> None:
        if self.cancelled and self._denial_id is None:
            raise self._unverified()

    def _unverified(self) -> ClaudeStreamError:
        return ClaudeStreamError(f"claude {self.input_kind} input lifecycle is unverified")
