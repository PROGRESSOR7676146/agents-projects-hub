"""Bounded optional mode observations, independent of execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_EARLY_GOAL_TURNS = 8
_GOAL_LABELS = {
    "active": "active",
    "paused": "paused",
    "blocked": "blocked",
    "usageLimited": "usage limited",
    "budgetLimited": "budget limited",
    "complete": "complete",
}


@dataclass(frozen=True, slots=True)
class CodexModeSnapshot:
    goal_status: str | None = None


def goal_mode_label(snapshot: CodexModeSnapshot | None) -> str | None:
    status = snapshot.goal_status if snapshot is not None else None
    label = _GOAL_LABELS.get(status) if isinstance(status, str) else None
    return f"/goal ({label}, observed)" if label is not None else None


def _bounded_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256


class CodexTurnModes:
    """Coalesce status only; never retain goal text, budgets or raw events.

    Before acceptance, at most eight candidate turn IDs can contribute. An
    exhausted bound retires only this optional observation until the next turn.
    A snapshot describes received exact-turn events, not a terminal-time or
    subsequently current session setting. No native read or mode change occurs.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._pending: dict[str, str | None] = {}
        self._goal_status: str | None = None
        self._retired = False

    def begin(self, thread_id: str) -> None:
        self.reset()
        if _bounded_id(thread_id):
            self._thread_id = thread_id

    def accept(self, turn_id: str) -> None:
        if not _bounded_id(turn_id):
            self._retire()
            return
        self._turn_id = turn_id
        self._goal_status = self._pending.get(turn_id) if not self._retired else None
        self._pending.clear()

    def _retire(self) -> None:
        self._retired = True
        self._pending.clear()
        self._goal_status = None

    def observe(self, message: dict[str, Any]) -> None:
        if self._retired or self._thread_id is None or "id" in message:
            return
        method = message.get("method")
        if method not in ("thread/goal/updated", "thread/goal/cleared"):
            return
        params = message.get("params")
        if not isinstance(params, dict) or params.get("threadId") != self._thread_id:
            return
        if method == "thread/goal/cleared":
            self._pending.clear()
            self._goal_status = None
            return
        turn_id = params.get("turnId")
        if not _bounded_id(turn_id):
            # An unbound update may change this thread's goal, but cannot
            # establish an exact-turn status or an explicit disabled state.
            self._pending.clear()
            self._goal_status = None
            return
        if self._turn_id is not None and turn_id != self._turn_id:
            return
        goal = params.get("goal")
        status = goal.get("status") if isinstance(goal, dict) else None
        if not (
            isinstance(goal, dict)
            and goal.get("threadId") == self._thread_id
            and isinstance(status, str)
            and status in _GOAL_LABELS
        ):
            status = None
        if self._turn_id is not None:
            self._goal_status = status
        else:
            assert isinstance(turn_id, str)
            if turn_id not in self._pending and len(self._pending) >= MAX_EARLY_GOAL_TURNS:
                self._retire()
                return
            self._pending[turn_id] = status

    def snapshot(self) -> CodexModeSnapshot:
        return CodexModeSnapshot(self._goal_status if self._turn_id is not None else None)
