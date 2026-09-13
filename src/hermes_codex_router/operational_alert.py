from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationalAlert:
    key: str
    code: str
    severity: str
    message: str
