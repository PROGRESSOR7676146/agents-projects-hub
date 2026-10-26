"""Failure finalization for one explicitly authorized, finite maintenance window.

No service discovery, shell, native-server start, state repair or retry authority.
The private procedure supplies fixed reviewed units and bounded callbacks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Literal

Observation = Literal["active", "inactive", "unknown"]


@dataclass(frozen=True)
class Unit:
    name: str
    admission: bool
    observe: Callable[[], Observation]
    stop: Callable[[], None]
    start: Callable[[], None]
    prerequisite: Callable[[], None]
    ready: Callable[[], None]


@dataclass(frozen=True)
class UnitResult:
    name: str
    outcome: Literal["ready", "blocked", "unknown"]
    diagnostic: str | None = None


@dataclass(frozen=True)
class RestorationReport:
    units: tuple[UnitResult, ...] = ()

    @property
    def complete(self) -> bool:
        return all(unit.outcome == "ready" for unit in self.units)


class RestorationError(RuntimeError):
    def __init__(self, report: RestorationReport):
        super().__init__("maintenance restoration incomplete; inspect report, do not retry")
        self.report = report


class MaintenanceWindow:
    """Wrap ALL stops and diagnostics in ``with``, not just the happy-path starts.

    Every callback must be bounded, record any effect intent durably BEFORE
    sending, and raise on missing/uncertain evidence. ``inactive`` means proven
    stopped, empty cgroup and no pending supervisor job; liveness alone is not
    ``ready``. Prerequisites include native custody and safe current config.
    Never include the native server in this restoration list. The caller owns
    the final private report and explicit human escalation for blocked units.
    """

    def __init__(self, units: Sequence[Unit]):
        if len({unit.name for unit in units}) != len(units):
            raise ValueError("duplicate maintenance unit")
        self.units = tuple(units)
        self.initial: dict[str, Observation] = {}
        self.stopped: set[str] = set()
        self.report = RestorationReport()
        self._entered = False
        self._finished = False

    def __enter__(self) -> MaintenanceWindow:
        if self._entered:
            raise ValueError("maintenance window is single use")
        self._entered = True
        for unit in self.units:
            state = unit.observe()
            if state not in ("active", "inactive"):
                raise ValueError("initial service state unknown")
            self.initial[unit.name] = state
        return self

    def stop(self, name: str) -> None:
        if not self._entered or self._finished or len(self.initial) != len(self.units):
            raise ValueError("maintenance window is not open")
        unit = next(unit for unit in self.units if unit.name == name)
        if self.initial[name] != "active":
            return
        if name in self.stopped:
            raise ValueError("stop already attempted; observe, never retry")
        if not unit.admission and any(
            other.observe() != "inactive"
            for other in self.units
            if other.admission and self.initial[other.name] == "active"
        ):
            raise ValueError("admission must be closed before stopping consumers")
        self.stopped.add(name)  # A stop may take effect even when its reply fails.
        unit.stop()

    def _restore_unit(self, unit: Unit) -> UnitResult:
        stage = "prerequisite"
        start_error = None
        try:
            unit.prerequisite()
            stage = "observe"
            state = unit.observe()
            if state == "inactive" and unit.name in self.stopped:
                try:
                    unit.start()  # At most one attempt, including timeout/unknown.
                except Exception as error:
                    start_error = "start:" + type(error).__name__
                state = unit.observe()
            if state != "active":
                return UnitResult(unit.name, "unknown", start_error or "observation")
            stage = "ready"
            unit.ready()
            return UnitResult(unit.name, "ready", start_error)
        except Exception as error:
            return UnitResult(unit.name, "blocked", stage + ":" + type(error).__name__)

    def restore(self) -> RestorationReport:
        if self._finished:
            return self.report
        self._finished = True  # No second effect even if finalization is called again.
        if not self.stopped:
            return self.report
        results = []
        consumers_ready = True
        # Check ALL previously active consumers, even ones not stopped by us.
        for unit in self.units:
            if unit.admission or self.initial.get(unit.name) != "active":
                continue
            result = self._restore_unit(unit)
            results.append(result)
            consumers_ready &= result.outcome == "ready"
        for unit in self.units:
            if not unit.admission or unit.name not in self.stopped:
                continue
            results.append(
                self._restore_unit(unit) if consumers_ready else UnitResult(unit.name, "blocked")
            )
        self.report = RestorationReport(tuple(results))
        return self.report

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        report = self.restore()
        if not report.complete:
            if exc is None:
                raise RestorationError(report)
            exc.add_note(
                "Maintenance restoration incomplete; inspect the separate restoration report"
            )
