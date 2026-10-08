"""Bounded exact-turn fail-safe after losing an accepted turn's control stream."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from .codex_appserver import StoredTurnOutcome
from .diagnostic_log import survived


class RecoveryControlClient(Protocol):
    def read_turn_outcome(
        self, *, thread_id: str, turn_id: str, cwd: Path, deadline: float | None = None
    ) -> StoredTurnOutcome: ...
    def interrupt_turn(
        self, *, thread_id: str, turn_id: str, deadline: float | None = None
    ) -> None: ...


def observe_after_control_loss(
    client: RecoveryControlClient,
    *,
    thread_id: str,
    turn_id: str,
    root: Path,
    may_interrupt: Callable[[], bool],
    on_interrupt_event: Callable[[Literal["attempted", "acknowledged", "unconfirmed"]], None]
    | None = None,
) -> StoredTurnOutcome:
    """Read, interrupt only proven active work once, then read independently.

    The caller owns the fresh no-fallback connection and validates its durable
    accepted checkpoint. There is no owner-stop receipt, resume, start or replay.
    Even a successful interrupt response does not establish terminality.
    """
    deadline = time.monotonic() + 15
    try:
        before = client.read_turn_outcome(
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=root,
            deadline=min(deadline, time.monotonic() + 5),
        )
    except Exception as error:
        survived("codex_control_recovery.read_unconfirmed", error)
        return StoredTurnOutcome("unknown")
    if before.status != "active":
        return before
    if not callable(getattr(client, "interrupt_turn", None)):
        return before
    try:
        if not may_interrupt():
            return before
    except Exception as error:
        survived("codex_control_recovery.guard_unconfirmed", error)
        return before
    try:
        if on_interrupt_event is not None:
            on_interrupt_event("attempted")
    except Exception as error:
        survived("codex_control_recovery.event_unconfirmed", error)
        return before
    event: Literal["acknowledged", "unconfirmed"] = "acknowledged"
    try:
        client.interrupt_turn(
            thread_id=thread_id,
            turn_id=turn_id,
            deadline=min(deadline, time.monotonic() + 5),
        )
    except Exception as error:
        event = "unconfirmed"
        survived("codex_control_recovery.interrupt_unconfirmed", error)
    try:
        if on_interrupt_event is not None:
            on_interrupt_event(event)
    except Exception as error:
        survived("codex_control_recovery.event_unconfirmed", error)
    try:
        return client.read_turn_outcome(
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=root,
            deadline=deadline,
        )
    except Exception as error:
        survived("codex_control_recovery.reread_unconfirmed", error)
        return StoredTurnOutcome("unknown")
