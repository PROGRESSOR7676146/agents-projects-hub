"""Bounded exact-turn fail-safe after losing an accepted turn's control stream."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Literal, Protocol

from .codex_appserver import RpcRejectedError, StoredTurnOutcome
from .codex_turn_controls import ActiveTurnProof, InterruptOutcome
from .diagnostic_log import survived
from .sqlite_contention import is_sqlite_contention


class RecoveryControlClient(Protocol):
    def close(self) -> None: ...
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
    begin_interrupt: Callable[[ActiveTurnProof, float], str | None],
    finish_interrupt: Callable[[str, InterruptOutcome], None],
    send_scope: Callable[[], AbstractContextManager[bool]] | None = None,
    on_interrupt_event: Callable[[Literal["attempted", "acknowledged", "unconfirmed"]], None]
    | None = None,
) -> StoredTurnOutcome:
    """Read, interrupt only proven active work once, then read independently.

    The caller owns the fresh no-fallback connection and durable send-start fence.
    There is no owner-stop receipt, resume, start or replay.
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
    proof = ActiveTurnProof(thread_id, turn_id, str(root), time.monotonic())

    def event(value: Literal["attempted", "acknowledged", "unconfirmed"]) -> None:
        try:
            if on_interrupt_event is not None:
                on_interrupt_event(value)
        except Exception as error:
            survived("codex_control_recovery.event_unconfirmed", error)

    with send_scope() if send_scope is not None else nullcontext(True) as permitted:
        if not permitted:
            return before
        try:
            owner = begin_interrupt(proof, deadline)
            if owner is None:
                return before
        except Exception as error:
            if is_sqlite_contention(error):
                raise  # No send exists; the live observer may retry its next poll.
            survived("codex_control_recovery.guard_unconfirmed", error)
            return before
        outcome: InterruptOutcome = "unknown"
        attempted = False
        try:
            # Only this branch proves that the client method was never called.
            # The owning transaction may have spent the proof's freshness
            # window before COMMIT, even with RPC budget still remaining.
            before_send = time.monotonic()
            if before_send >= deadline or not 0 <= before_send - proof.observed_monotonic <= 5:
                outcome = "not_sent"
            else:
                attempted = True
                client.interrupt_turn(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    deadline=min(deadline, time.monotonic() + 5),
                )
                outcome = "matched_ack"
        except RpcRejectedError as error:
            outcome = "matched_rejection"
            survived("codex_control_recovery.interrupt_rejected", error)
        except Exception as error:
            survived("codex_control_recovery.interrupt_unconfirmed", error)
        finally:
            _settle_interrupt(finish_interrupt, owner, outcome)
    # Optional diagnostic writes must never sit between the fence and RPC.
    if attempted:
        event("attempted")
    event("acknowledged" if outcome == "matched_ack" else "unconfirmed")
    if time.monotonic() >= deadline:
        return StoredTurnOutcome("unknown")
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


def _settle_interrupt(
    finish: Callable[[str, InterruptOutcome], None], owner: str, outcome: InterruptOutcome
) -> None:
    """Retain matched evidence in memory across bounded state-only contention."""
    deadline = time.monotonic() + 2
    while True:
        try:
            finish(owner, outcome)
            return
        except Exception as error:
            remaining = deadline - time.monotonic()
            if not is_sqlite_contention(error) or remaining <= 0:
                survived("codex_control_recovery.settlement_unconfirmed", error)
                return
            time.sleep(min(0.05, remaining))
