from __future__ import annotations

import queue
import signal
import threading
from contextlib import contextmanager
from types import FrameType
from typing import Callable, Iterator, Protocol

from .diagnostic_log import survived

_STOP_JOIN_TIMEOUT_SECONDS = 5


class Stoppable(Protocol):
    def stop(self) -> None: ...


@contextmanager
def stop_on_signals(
    component: Stoppable, *, cleanup: Callable[[], None] | None = None
) -> Iterator[None]:
    """Translate process termination signals into a component stop request.

    Python permits installing signal handlers only in the main thread.  Tests
    and embedded callers may invoke the CLI from another thread, where this
    context deliberately becomes a no-op. A single daemon thread is started
    before the handlers are installed. The handler only queues a request; it
    performs no I/O, state transition, joining, or provider cleanup. Requests
    are serialized and coalesced after the first successful stop. A failed stop
    is reported and a later request can retry it. On exit, a bounded join waits
    for the stopper before optional cleanup, with handlers installed until the
    cleanup finishes. Further signals are ignored during cleanup. If the join
    times out, cleanup is skipped and an error is raised; the daemon thread may
    still be running. Cleanup also runs if the stopper cannot be started.
    """

    if threading.current_thread() is not threading.main_thread():
        try:
            yield
        finally:
            if cleanup is not None:
                cleanup()
        return

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    requests: queue.SimpleQueue[int | None] = queue.SimpleQueue()

    def stop_component() -> None:
        stopped = False
        while True:
            request = requests.get()
            if request is None:
                return
            exiting = False
            while True:
                try:
                    pending = requests.get_nowait()
                except queue.Empty:
                    break
                if pending is None:
                    exiting = True
                    break
            if not stopped:
                try:
                    component.stop()
                except BaseException as error:  # keep the signal bridge available for a retry
                    survived("lifecycle.stop", error)
                else:
                    stopped = True
            if exiting:
                return

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        # CPython's SimpleQueue.put is reentrant, including within a handler.
        requests.put_nowait(signum)

    stopper = threading.Thread(target=stop_component, name="signal-stopper", daemon=True)
    started = False
    installed = []
    try:
        stopper.start()
        started = True
        for signum in previous:
            signal.signal(signum, request_stop)
            installed.append(signum)
        yield
    finally:
        try:
            if started:
                requests.put_nowait(None)
                stopper.join(_STOP_JOIN_TIMEOUT_SECONDS)
                if stopper.is_alive():
                    raise RuntimeError("signal stopper did not finish before cleanup")
            if cleanup is not None:
                cleanup()
        finally:
            if threading.current_thread() is threading.main_thread():
                for signum in installed:
                    signal.signal(signum, previous[signum])
