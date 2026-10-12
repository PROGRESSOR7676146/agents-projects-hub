"""Delay owned-client shutdown across one fenced control send and settlement."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class ControlSendPath:
    def __init__(self, close: Callable[[], None]) -> None:
        self.close = close
        self.lock = threading.Lock()
        self.sending = False
        self.close_requested = False
        self.closed = False

    def request_close(self) -> None:
        with self.lock:
            self.close_requested = True
            close = not self.sending and not self.closed
            if close:
                self.closed = True
        if close:
            self.close()

    @contextmanager
    def sending_scope(self) -> Iterator[bool]:
        # Reserve before acquiring the durable fence. The lock never spans
        # SQLite or RPC. A concurrent shutdown cannot destroy a matched reply.
        with self.lock:
            permitted = not self.close_requested and not self.sending
            if permitted:
                self.sending = True
        try:
            yield permitted
        finally:
            if permitted:
                with self.lock:
                    self.sending = False
                    close = self.close_requested and not self.closed
                    if close:
                        self.closed = True
                if close:
                    self.close()
