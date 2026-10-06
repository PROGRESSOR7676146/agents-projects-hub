"""Single-use proof for retiring a disposable completed socket connection."""

from __future__ import annotations

from typing import Any


class CompletedConnectionProof:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._identity: tuple[str, str] | None = None

    def invalidate(self) -> None:
        self._identity = None

    def observe(
        self, *, thread_id: str | None, turn_id: str | None, params: dict[str, Any]
    ) -> None:
        turn = params.get("turn")
        if (
            self.enabled
            and thread_id is not None
            and turn_id is not None
            and params.get("threadId") == thread_id
            and isinstance(turn, dict)
            and turn.get("id") == turn_id
            and turn.get("status") == "completed"
        ):
            self._identity = (thread_id, turn_id)

    def consume(self, *, thread_id: str, turn_id: str) -> bool:
        identity, self._identity = self._identity, None
        return self.enabled and identity == (thread_id, turn_id)
