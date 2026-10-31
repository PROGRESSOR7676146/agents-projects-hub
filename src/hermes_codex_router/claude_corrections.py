"""Bounded native streamed text input, with exact processed receipts.

The process/permission host remains owned by the original job. A caller UUID
identifies an input, never a native turn. Receipt order against result records
distinguishes a correction consumed in result 1 from a late native turn 2.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable

from .claude_stream import MAX_CLAUDE_VISIBLE_CHARACTERS, ClaudeStreamError

MAX_CORRECTION_CHARACTERS = 20_000


class ClaudeAdmissionClosed(Exception):
    """No input was reserved; preserve the parent and use ordinary FIFO."""


@dataclass(frozen=True, slots=True)
class ClaudeInputFrame:
    uuid: str
    session_id: str
    text: str

    def encode(self) -> bytes:
        if (
            str(uuid.UUID(self.uuid)) != self.uuid
            or str(uuid.UUID(self.session_id)) != self.session_id
            or not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > 200_000
        ):
            raise ClaudeStreamError("claude text input is invalid")
        return (
            json.dumps(
                {
                    "type": "user",
                    "uuid": self.uuid,
                    "session_id": self.session_id,
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": self.text},
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")


@dataclass(slots=True)
class ClaudeCorrectionCallbacks:
    prepare: Callable[[ClaudeInputFrame], None]
    claim: Callable[[], ClaudeInputFrame | None]
    receipt: Callable[[str, int], None]
    result: Callable[[int], None]


class ClaudeCorrectionInput:
    """One initial input and at most one durable FIFO correction per launch."""

    def __init__(self, prompt: str, session_id: str, callbacks: ClaudeCorrectionCallbacks):
        self.callbacks = callbacks
        self.initial = ClaudeInputFrame(str(uuid.uuid4()), session_id, prompt)
        self.initial_data = self.initial.encode()
        self.frames = [self.initial]
        self.receipts: list[int] = []
        self.results: list[dict[str, object]] = []
        self.lifecycle: dict[str, list[str]] = {}
        self.sealed = False
        self.complete = False
        self._persist(lambda: callbacks.prepare(self.initial))

    @staticmethod
    def _persist(operation: Callable[[], object]) -> object:
        try:
            return operation()
        except ClaudeAdmissionClosed:
            raise
        except Exception as exc:
            raise ClaudeStreamError("claude text input persistence failed") from exc

    def poll(self) -> bytes | None:
        if self.sealed or len(self.frames) == 2:
            return None
        try:
            frame = self._persist(self.callbacks.claim)
        except ClaudeAdmissionClosed:
            self.sealed = True
            return None
        if frame is None:
            return None
        if not isinstance(frame, ClaudeInputFrame) or frame.session_id != self.initial.session_id:
            raise ClaudeStreamError("claude correction binding changed")
        data = frame.encode()
        if len(frame.text) > MAX_CORRECTION_CHARACTERS or frame.uuid == self.initial.uuid:
            raise ClaudeStreamError("claude correction exceeded its input contract")
        self.frames.append(frame)
        return data

    def observe(self, event: dict[str, object]) -> bool:
        kind = event.get("type")
        if kind == "command_lifecycle":
            identifier = event.get("command_uuid")
            known = {frame.uuid for frame in self.frames}
            if not isinstance(identifier, str) or identifier not in known:
                raise ClaudeStreamError("claude text input lifecycle is unverified")
            history = self.lifecycle.setdefault(identifier, [])
            sequence = ["queued", "started", "completed"]
            if (
                set(event) != {"type", "state", "session_id", "command_uuid", "uuid"}
                or event.get("session_id") != self.initial.session_id
                or len(history) >= 3
                or event.get("state") != sequence[len(history)]
                or self.complete
                and event.get("state") != "completed"
            ):
                raise ClaudeStreamError("claude text input lifecycle is unverified")
            history.append(sequence[len(history)])
            return False
        if kind == "user" and event.get("isReplay") is True:
            if len(self.receipts) >= len(self.frames) or self.complete:
                raise ClaudeStreamError("claude text input receipt is duplicated")
            frame = self.frames[len(self.receipts)]
            if (
                event.get("uuid") != frame.uuid
                or event.get("session_id") != frame.session_id
                or "parent_tool_use_id" not in event
                or event["parent_tool_use_id"] is not None
                or event.get("message") != {"role": "user", "content": frame.text}
            ):
                raise ClaudeStreamError("claude text input receipt conflicts with submission")
            boundary = len(self.results) + 1
            self._persist(lambda: self.callbacks.receipt(frame.uuid, boundary))
            self.receipts.append(boundary)
            return False
        if kind == "user":
            # Native tool results are user events too. They remain subject to
            # the selected tool policy and must never count as input receipts.
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if (
                event.get("isReplay") is not None
                or not isinstance(content, list)
                or not content
                or any(
                    not isinstance(block, dict) or block.get("type") != "tool_result"
                    for block in content
                )
            ):
                raise ClaudeStreamError("claude text input receipt is unverified")
            return True
        if kind != "result":
            return True
        self.sealed = True
        if self.complete or not self.receipts or self.receipts[0] != 1 or len(self.results) >= 2:
            raise ClaudeStreamError("claude text input result boundary is unverified")
        if len(self.results) and not any(boundary == 2 for boundary in self.receipts):
            raise ClaudeStreamError("claude returned an unowned subsequent result")
        # ClaudeStreamReader validates the native result before this method.
        self.results.append(dict(event))
        self._persist(lambda: self.callbacks.result(len(self.results)))
        self.complete = len(self.receipts) == len(self.frames) and max(self.receipts) <= len(
            self.results
        )
        if not self.complete:
            return False
        if len(self.frames) == 2 and event.get("is_error") is not False:
            raise ClaudeStreamError("claude correction has an incomplete terminal outcome")
        if len(self.results) == 2:
            if any(item.get("is_error") is not False for item in self.results):
                raise ClaudeStreamError("claude correction has an incomplete terminal outcome")
            aggregate = "\n\n".join(str(item["result"]) for item in self.results)
            if len(aggregate) > MAX_CLAUDE_VISIBLE_CHARACTERS:
                raise ClaudeStreamError("claude aggregate result exceeded its limit")
            event["result"] = aggregate
        return True

    def finish(self) -> None:
        if not self.complete:
            raise ClaudeStreamError("claude text input has no complete processed result")
