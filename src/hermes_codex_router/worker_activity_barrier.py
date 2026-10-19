"""In-process admission and physical-operation accounting for one worker.

Drain is monotonic. It does not stop a provider, prove native terminality, close
cached clients, resolve durable claims, or authorize process/service actions.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class WorkerActivityBarrier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._local = threading.local()
        self._draining = False
        self._active = 0
        self._threads: set[threading.Thread] = set()

    def request_drain(self) -> None:
        with self._lock:
            self._draining = True

    @property
    def draining(self) -> bool:
        with self._lock:
            return self._draining

    @property
    def drained(self) -> bool:
        with self._lock:
            self._threads = {thread for thread in self._threads if thread.is_alive()}
            return self._draining and self._active == 0 and not self._threads

    def _retain(self) -> bool:
        with self._lock:
            if self._draining and not getattr(self._local, "depth", 0):
                return False
            self._active += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    @contextmanager
    def operation(self) -> Iterator[bool]:
        """Admit an entire claim/execute/settle/cleanup operation atomically."""
        admitted = self._retain()
        if not admitted:
            yield False
            return
        self._local.depth = getattr(self._local, "depth", 0) + 1
        try:
            yield True
        finally:
            self._local.depth -= 1
            self._release()

    def thread(
        self, *, target: Callable[[], object], name: str | None = None, daemon: bool = True
    ) -> threading.Thread:
        """Count before start; only the actual target exit releases activity.

        A child of admitted work can finish after drain. A long-lived loop must
        independently gate each claim; the thread never inherits admission.
        """
        return _ActivityThread(self, target=target, name=name, daemon=daemon)


class _ActivityThread(threading.Thread):
    def __init__(
        self,
        activity: WorkerActivityBarrier,
        *,
        target: Callable[[], object],
        name: str | None,
        daemon: bool,
    ) -> None:
        super().__init__(name=name, daemon=daemon)
        self._activity = activity
        self._work = target
        self._start_lock = threading.Lock()
        self._attempted = False

    def start(self) -> None:
        with self._start_lock:
            if self._attempted:
                raise RuntimeError("activity thread start already attempted")
            self._attempted = True
            if not self._activity._retain():
                raise RuntimeError("worker is draining")
            with self._activity._lock:
                self._activity._threads = {
                    thread for thread in self._activity._threads if thread.is_alive()
                }
                self._activity._threads.add(self)
            # An exception can occur after the OS thread was created. Only
            # target exit can release its reservation; otherwise retain unknown
            # startup activity rather than manufacture a drain acknowledgement.
            super().start()

    def run(self) -> None:
        try:
            self._work()
        finally:
            self._activity._release()
