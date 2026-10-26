"""Bounded process-memory consent; never loaded from native settings or disk."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Hashable


class BashGrantCache:
    """One worker owns this cache; an authenticated tlive epoch fences each use."""

    def __init__(self) -> None:
        self._grants: OrderedDict[Hashable, str] = OrderedDict()
        self._lock = threading.Lock()

    def allows(self, scope: Hashable, epoch: str) -> bool:
        with self._lock:
            previous = self._grants.get(scope)
            if previous != epoch:
                self._grants.pop(scope, None)
                return False
            return True

    def remember(self, scope: Hashable, epoch: str) -> None:
        with self._lock:
            self._grants[scope] = epoch
            self._grants.move_to_end(scope)
            while len(self._grants) > 256:
                self._grants.popitem(last=False)  # Eviction only removes authority.


BASH_GRANTS = BashGrantCache()
