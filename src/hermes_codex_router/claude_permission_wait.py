"""Trusted host wait accounting; native stream text cannot pause a deadline."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

HUMAN_PERMISSION_SECONDS = 7 * 24 * 3600
PERMISSION_HOOK_SECONDS = HUMAN_PERMISSION_SECONDS + 10
NATIVE_PERMISSION_HOOK_SECONDS = PERMISSION_HOOK_SECONDS + 10


class PermissionWaitClock:
    """One per launch, shared only by the host and its owned process runner."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._started: float | None = None
        self._elapsed = 0.0

    @contextmanager
    def waiting(self) -> Iterator[None]:
        with self._lock:
            if self._started is not None:
                raise RuntimeError("overlapping Claude permission waits")
            self._started = self._clock()
        try:
            yield
        finally:
            with self._lock:
                assert self._started is not None
                self._elapsed += self._clock() - self._started
                self._started = None

    def elapsed(self) -> float:
        with self._lock:
            current = 0.0 if self._started is None else self._clock() - self._started
            return self._elapsed + current


class ProductiveDeadline:
    """Keep execution finite while excluding verified host permission waits."""

    def __init__(
        self,
        seconds: float,
        wait: PermissionWaitClock | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._wait = wait
        self._initial_wait = 0.0 if wait is None else wait.elapsed()
        self._deadline = clock() + seconds
        self._stop_deadline: float | None = None

    def remaining(self) -> float:
        waiting = 0.0 if self._wait is None else self._wait.elapsed() - self._initial_wait
        deadline = self._deadline + max(0.0, waiting)
        if self._stop_deadline is not None:
            deadline = min(deadline, self._stop_deadline)
        return deadline - self._clock()

    def stop_within(self, seconds: float) -> None:
        self._stop_deadline = self._clock() + max(0.0, min(seconds, self.remaining()))
