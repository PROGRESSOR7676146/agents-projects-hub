"""Bounded read-only consumption after a known stdio response-channel fault."""

from __future__ import annotations

from typing import Any

from .codex_rpc import RpcError, RpcOutboundUnavailableError

RESPONSE_FAILURE_DRAIN_SECONDS = 20.0
RESPONSE_FAILURE_NOTICE = (
    "Hub transport notice: the response channel failed. Hub granted no permission."
)


class CodexResponseDrain:
    def __init__(self) -> None:
        self.deadline: float | None = None

    def record(self, *, now: float) -> None:
        if self.deadline is None:
            self.deadline = now + RESPONSE_FAILURE_DRAIN_SECONDS

    def observe(self, transport: object, *, now: float) -> None:
        # An ordinary reader EOF is not evidence of a failed response. Physical
        # writes can fail after send() has returned successful queue admission.
        if isinstance(getattr(transport, "writer_failure", None), RpcOutboundUnavailableError):
            self.record(now=now)

    def remaining(self, *, now: float, seconds: float) -> float:
        if self.deadline is None:
            return seconds
        remaining = self.deadline - now
        if remaining <= 0:
            raise RpcError("Codex response-channel drain deadline exceeded")
        return min(seconds, remaining)

    def annotate(self, text: str) -> str:
        if self.deadline is None:
            return text
        return "\n\n".join(part for part in (text, RESPONSE_FAILURE_NOTICE) if part)

    def proves_completed(
        self,
        params: dict[str, Any],
        *,
        thread_id: str | None,
        turn_id: str,
        accepted_turn_id: str | None,
    ) -> bool:
        if self.deadline is None:
            return True  # Retain the ordinary older-server compatibility path.
        turn = params.get("turn")
        return (
            thread_id is not None
            and bool(accepted_turn_id)
            and accepted_turn_id == turn_id
            and params.get("threadId") == thread_id
            and isinstance(turn, dict)
            and turn.get("id") == turn_id
            and turn.get("status") == "completed"
        )
