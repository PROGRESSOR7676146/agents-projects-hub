"""Dependency-neutral human assessment input; no provider or transport authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

_RESERVED = re.compile(r"^/assess(?:@[A-Za-z0-9_]+)?(?:\s|$)", re.IGNORECASE)
_COMMAND = re.compile(
    r"^/assess(?:@[A-Za-z0-9_]+)?\s+(accepted|rework|unknown)\s+(.+)$", re.IGNORECASE | re.DOTALL
)


def is_assessment_command(text: str) -> bool:
    return _RESERVED.match(text.strip()) is not None


@dataclass(frozen=True, slots=True)
class OutcomeAssessmentInput:
    owner_user_id: int
    chat_id: int
    thread_id: int
    message_id: int
    reply_message_id: int | None
    text: str
    text_source: str = "text"
    is_forwarded: bool = False
    quote_text: str | None = None
    has_material: bool = False
    material_fingerprint: str | None = None
    transport_message_fingerprint: str | None = None

    def fingerprint(self) -> str:
        # Hash full raw input before bounded parsing; changed input must not alias.
        encoded = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def parsed(self) -> tuple[str, str] | None:
        matched = _COMMAND.fullmatch(self.text.strip())
        if matched is None:
            return None
        reason = matched[2].strip()
        if not 1 <= len(reason) <= 500:
            return None
        return matched[1].casefold(), reason
