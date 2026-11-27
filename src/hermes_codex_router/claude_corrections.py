"""Bounded native streamed text input, with exact processed receipts.

The process/permission host remains owned by the original job. A caller UUID
identifies an input, never a native turn. Receipt order against result records
distinguishes a correction consumed in result 1 from a late native turn 2.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .claude_input_lifecycle import ClaudeInputLifecycle
from .claude_read_image import ClaudeReadImage
from .claude_stream import MAX_CLAUDE_VISIBLE_CHARACTERS, ClaudeStreamError

if TYPE_CHECKING:
    from .claude_image_input import VerifiedClaudeImage
    from .claude_image_receipt import ClaudeImageReceipt

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

    def __init__(
        self,
        prompt: str,
        session_id: str,
        callbacks: ClaudeCorrectionCallbacks,
        images: Sequence[VerifiedClaudeImage] = (),
    ):
        self.callbacks = callbacks
        self._image_receipt: ClaudeImageReceipt | None = None
        if images:
            # Images travel only in the initial frame, with the same bounded
            # encoding and replay check as the tools-disabled image path.
            from .claude_image_input import ClaudeImageInputError, encode_claude_image_input
            from .claude_image_receipt import ClaudeImageReceipt

            try:
                self.initial_data = encode_claude_image_input(prompt, images, session_id)
                self._image_receipt = ClaudeImageReceipt(
                    self.initial_data, expected_session_id=session_id
                )
            except ClaudeImageInputError as exc:
                raise ClaudeStreamError("claude image input could not be prepared") from exc
            identity = json.loads(self.initial_data)["uuid"]
            self.initial = ClaudeInputFrame(identity, session_id, prompt)
        else:
            self.initial = ClaudeInputFrame(str(uuid.uuid4()), session_id, prompt)
            self.initial_data = self.initial.encode()
        self.frames = [self.initial]
        self.receipts: list[int] = []
        self.results: list[dict[str, object]] = []
        self.lifecycle = ClaudeInputLifecycle(session_id, self.initial.uuid, "text")
        self.read_image = ClaudeReadImage(session_id)
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

    @property
    def has_images(self) -> bool:
        return self._image_receipt is not None

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
        if not isinstance(kind, str):
            raise ClaudeStreamError("claude returned an invalid event type")
        input_ids = {frame.uuid for frame in self.frames}
        if self.read_image.observe(event, input_ids=input_ids, acknowledged=bool(self.receipts)):
            self.lifecycle.observe_activity(event)
            return False
        if kind == "command_lifecycle":
            self.lifecycle.observe_command(
                event, input_ids=input_ids, acknowledged=bool(self.receipts), terminal=self.complete
            )
            if self.lifecycle.cancelled:
                self.sealed = True
            return False
        if (
            kind == "user"
            and (event.get("isReplay") is not True or event.get("parent_tool_use_id") is not None)
            or kind
            in {"assistant", "stream_event", "tool_progress", "tool_use_summary", "control_request"}
        ):
            self.lifecycle.observe_activity(event)
        if kind == "user" and event.get("parent_tool_use_id") is not None:
            # A forwarded subagent message names the Agent call that owns it
            # (headless docs: parent_tool_use_id). It is tool-internal, never a
            # receipt of our submission. Event policies that forbid children
            # (text-only, confined file tools) reject it before this point.
            parent = event["parent_tool_use_id"]
            if not isinstance(parent, str) or not parent.strip():
                raise ClaudeStreamError("claude subagent message ownership is invalid")
            return True
        if kind == "user" and event.get("isReplay") is True:
            if len(self.receipts) >= len(self.frames) or self.complete:
                raise ClaudeStreamError("claude text input receipt is duplicated")
            frame = self.frames[len(self.receipts)]
            if self.lifecycle.cancelled:
                raise ClaudeStreamError("claude text input receipt followed cancellation")
            if frame is self.initial and self._image_receipt is not None:
                self._image_receipt.observe(event)
            elif (
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
        if event.get("parent_tool_use_id") is not None:
            # Only the submission's own native turn may complete it.
            raise ClaudeStreamError("claude subagent result cannot complete the turn")
        self.sealed = True
        if self.complete or not self.receipts or self.receipts[0] != 1 or len(self.results) >= 2:
            raise ClaudeStreamError("claude text input result boundary is unverified")
        if len(self.results) and not any(boundary == 2 for boundary in self.receipts):
            raise ClaudeStreamError("claude returned an unowned subsequent result")
        # ClaudeStreamReader validates the native result before this method.
        self.lifecycle.observe_result(event)
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

    def diagnostic(self, event: dict[str, object]) -> str:
        """Counts and lifecycle role for a rejected event; never IDs or text."""
        detail = (
            f"input inputs={len(self.frames)} receipts={len(self.receipts)} "
            f"results={len(self.results)}"
        )
        if event.get("type") != "command_lifecycle":
            return detail
        identifier = event.get("command_uuid")
        role = (
            "initial"
            if identifier == self.initial.uuid
            else "correction"
            if any(identifier == frame.uuid for frame in self.frames[1:])
            else "unknown"
        )
        history = (
            "+".join(self.lifecycle.history.get(identifier, ()))
            if isinstance(identifier, str)
            else ""
        )
        return f"{detail} command={role} history={history or '-'}"

    def finish(self) -> None:
        self.lifecycle.finish()
        if not self.complete:
            raise ClaudeStreamError("claude text input has no complete processed result")
