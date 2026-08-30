from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from types import FrameType
from typing import Iterator, Protocol


class Stoppable(Protocol):
    def stop(self) -> None: ...


@contextmanager
def stop_on_signals(component: Stoppable) -> Iterator[None]:
    """Translate process termination signals into a component stop request.

    Python permits installing signal handlers only in the main thread.  Tests
    and embedded callers may invoke the CLI from another thread, where this
    context deliberately becomes a no-op.  The handler itself performs no I/O,
    state transition, joining, or provider cleanup; those remain in the normal
    run-loop and ``close`` path.
    """

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        component.stop()

    for signum in previous:
        signal.signal(signum, request_stop)
    try:
        yield
    finally:
        # The context entered on the main thread, and ordinary CLI use exits on
        # that same thread.  Keep the guard for unusual generator/context use
        # so tests never attempt an illegal signal restoration.
        if threading.current_thread() is threading.main_thread():
            for signum, handler in previous.items():
                signal.signal(signum, handler)
