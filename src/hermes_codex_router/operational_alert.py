from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationalAlert:
    key: str
    code: str
    severity: str
    message: str
    # ADR 0084 impact class; None until classify_alerts resolves it.
    impact: str | None = None
