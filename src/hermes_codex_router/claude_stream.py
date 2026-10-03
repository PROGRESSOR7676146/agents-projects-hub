from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable

MAX_CLAUDE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CLAUDE_VISIBLE_CHARACTERS = 200_000
MAX_CLAUDE_EVENTS = 512
MAX_CLAUDE_STDERR_BYTES = 64 * 1024


class ClaudeStreamError(RuntimeError):
    """The structured stream does not prove an exact terminal outcome."""


class ClaudeTerminalFailure(RuntimeError):
    """A verified native terminal failure; no raw diagnostics or quota estimates."""

    def __init__(self, code: str, public_message: str, session_id: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.session_id = session_id


@dataclass(frozen=True, slots=True)
class ClaudeParsedResult:
    text: str
    session_id: str
    model: str | None


@dataclass(frozen=True, slots=True)
class ClaudeVisibleAssistant:
    """Completed native message text; provisional until the whole turn validates.

    Message UUID is not a provider turn ID. No reasoning or tool data is exposed.
    """

    session_id: str
    message_id: str
    text: str


VisibleAssistantCallback = Callable[[ClaudeVisibleAssistant], None]


def _event(line: str) -> dict[str, object]:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError) as exc:
        raise ClaudeStreamError("claude returned malformed structured output") from exc
    if not isinstance(event, dict):
        raise ClaudeStreamError("claude returned a non-object event")
    _require_text_only_event(event)
    return event


def _require_text_only_event(event: dict[str, object]) -> None:
    """Refuse observed capability drift before provisional text or completion.

    CLI flags remain the preventive boundary. Stream observations are only a
    fail-closed backstop, never proof that an unexpected action did not run.
    Missing optional metadata is not an attestation of tool or hook isolation.
    """
    kind = event.get("type")
    subtype = event.get("subtype")
    violated = isinstance(kind, str) and kind in {
        "control_request",
        "control_response",
        "tool_progress",
        "tool_use_summary",
        "hook_started",
        "hook_progress",
        "hook_response",
        "task_started",
        "task_progress",
        "task_notification",
    }
    if kind == "system" and isinstance(subtype, str):
        violated |= subtype.startswith("hook_") or subtype in {
            "task_started",
            "task_progress",
            "task_notification",
        }
        if subtype == "init":
            for field in ("tools", "mcp_servers", "plugins", "skills"):
                if field in event:
                    value = event[field]
                    violated |= not isinstance(value, list) or bool(value)
            if "permissionMode" in event:
                violated |= event["permissionMode"] != "dontAsk"
    if event.get("parent_tool_use_id") is not None:
        violated = True
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        violated |= any(_tool_block(block) for block in content)
    streamed = event.get("event")
    if kind == "stream_event" and isinstance(streamed, dict):
        violated |= _tool_block(streamed.get("content_block"))
        delta = streamed.get("delta")
        violated |= isinstance(delta, dict) and delta.get("type") == "input_json_delta"
    if violated:
        # Never include a tool name, hook text, command, request ID or payload.
        raise ClaudeStreamError("claude text-only runtime policy was violated")


def _tool_block(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    return isinstance(kind, str) and (kind.endswith("tool_use") or kind.endswith("tool_result"))


class ClaudeStreamReader:
    """Bound raw bytes and validate each complete NDJSON event while draining."""

    def __init__(
        self,
        *,
        expected_session_id: str | None = None,
        on_visible_assistant: VisibleAssistantCallback | None = None,
    ) -> None:
        self.session_id = expected_session_id
        self.on_visible_assistant = on_visible_assistant
        self._output = bytearray()
        self._pending = bytearray()
        self._events = 0
        self._terminal_seen = False
        self._visible: dict[str, str] = {}
        self._visible_characters = 0

    def feed(self, chunk: bytes) -> None:
        if len(self._output) + len(chunk) > MAX_CLAUDE_OUTPUT_BYTES:
            raise ClaudeStreamError("claude structured output exceeded its limit")
        self._output.extend(chunk)
        self._pending.extend(chunk)
        while (end := self._pending.find(b"\n")) >= 0:
            line = bytes(self._pending[:end])
            del self._pending[: end + 1]
            self._line(line)

    def finish(self) -> str:
        if self._pending:
            self._line(bytes(self._pending))
            self._pending.clear()
        try:
            return self._output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClaudeStreamError("claude returned invalid structured encoding") from exc

    def _line(self, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClaudeStreamError("claude returned invalid structured encoding") from exc
        if not line.strip():
            return
        event = _event(line)
        self._events += 1
        if self._events > MAX_CLAUDE_EVENTS:
            raise ClaudeStreamError("claude structured output exceeded its event limit")
        if "session_id" in event:
            identity = _session_id(event["session_id"])
            if self.session_id is not None and self.session_id != identity:
                raise ClaudeStreamError("claude stream session identity changed")
            self.session_id = identity
        if self._terminal_seen and event.get("type") != "prompt_suggestion":
            raise ClaudeStreamError("claude returned events after its terminal outcome")
        if event.get("type") == "result":
            self._terminal_seen = True
        if self.on_visible_assistant is not None:
            self._visible_assistant(event)

    def _visible_assistant(self, event: dict[str, object]) -> None:
        # Native assistant messages are complete; stream_event deltas are not.
        # Public schema: agent-sdk/typescript#sdkassistantmessage; headless docs
        # distinguish main parent_tool_use_id=null from forwarded subagents.
        if (
            event.get("type") != "assistant"
            or event.get("error") is not None
            or event.get("aborted") is not None
        ):
            return
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return
        text = "\n".join(
            block["text"]
            for block in blocks
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        if not text.strip():
            return
        if "parent_tool_use_id" not in event:
            raise ClaudeStreamError("claude visible message ownership is missing")
        parent = event["parent_tool_use_id"]
        if parent is not None:
            if not isinstance(parent, str) or not parent.strip():
                raise ClaudeStreamError("claude visible message ownership is invalid")
            return
        identity = _session_id(event.get("session_id"))
        message_id = _session_id(event.get("uuid"))
        if message_id in self._visible:
            if self._visible[message_id] != text:
                raise ClaudeStreamError("claude completed visible message changed")
            return
        if self._visible_characters + len(text) > MAX_CLAUDE_VISIBLE_CHARACTERS:
            raise ClaudeStreamError("claude visible messages exceeded their limit")
        assert self.on_visible_assistant is not None
        try:
            self.on_visible_assistant(ClaudeVisibleAssistant(identity, message_id, text))
        except Exception as exc:
            raise ClaudeStreamError("claude visible message persistence failed") from exc
        self._visible[message_id] = text
        self._visible_characters += len(text)


def _session_id(value: object) -> str:
    if not isinstance(value, str):
        raise ClaudeStreamError("claude returned no session id")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise ClaudeStreamError("claude returned an invalid session id") from exc
    return value


def _terminal_failure_code(terminal: dict[str, object], assistant_error: str | None) -> str:
    subtype_codes = {
        "error_max_turns": "claude_turn_limit",
        "error_max_budget_usd": "claude_budget_exhausted",
        "error_max_structured_output_retries": "claude_structured_output_failed",
    }
    subtype = terminal.get("subtype")
    if isinstance(subtype, str) and subtype in subtype_codes:
        return subtype_codes[subtype]
    status = terminal.get("api_error_status")
    if type(status) is int and status == 429:
        return "claude_quota_exhausted"
    if type(status) is int and status == 401:
        return "claude_authentication_failed"
    error_codes = {
        "rate_limit": "claude_quota_exhausted",
        "authentication_failed": "claude_authentication_failed",
        "billing_error": "claude_billing_error",
        "overloaded": "claude_provider_overloaded",
        "model_not_found": "claude_model_not_found",
    }
    return error_codes.get(assistant_error or "", "claude_provider_failure")


def parse_claude_stream(
    output: str,
    *,
    expected_session_id: str | None = None,
    requested_model: str | None = None,
    returncode: int = 0,
) -> ClaudeParsedResult:
    """Validate the entire bounded NDJSON stream before accepting its result.

    Earlier errors cannot override a successful terminal result. A conflicting
    later event invalidates even an otherwise well-formed terminal outcome.
    """
    if (
        len(output) > MAX_CLAUDE_OUTPUT_BYTES
        or len(output.encode("utf-8")) > MAX_CLAUDE_OUTPUT_BYTES
    ):
        raise ClaudeStreamError("claude structured output exceeded its limit")
    events: list[dict[str, object]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        events.append(_event(line))
        if len(events) > MAX_CLAUDE_EVENTS:
            raise ClaudeStreamError("claude structured output exceeded its event limit")
    terminals = [event for event in events if event.get("type") == "result"]
    if len(terminals) != 1:
        raise ClaudeStreamError("claude returned no unique terminal result")
    terminal = terminals[0]
    terminal_index = events.index(terminal)
    if any(event.get("type") != "prompt_suggestion" for event in events[terminal_index + 1 :]):
        raise ClaudeStreamError("claude returned events after its terminal outcome")
    session_id = _session_id(terminal.get("session_id"))
    if expected_session_id is not None and session_id != expected_session_id:
        raise ClaudeStreamError("claude returned a different session")
    initialized_model: str | None = None
    answered_model: str | None = None
    assistant_error: str | None = None
    for event in events:
        if "session_id" in event and _session_id(event["session_id"]) != session_id:
            raise ClaudeStreamError("claude stream session identity changed")
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str) and model.strip():
                initialized_model = model.strip()[:200]
        if event.get("type") == "assistant":
            # Only the latest assistant event, bound to this exact session, can
            # explain a terminal error. Earlier retry failures are not proof.
            error = event.get("error")
            assistant_error = (
                error if event.get("session_id") == session_id and isinstance(error, str) else None
            )
            message = event.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if isinstance(model, str) and model.strip():
                answered_model = model.strip()[:200]
    subtype = terminal.get("subtype")
    is_error = terminal.get("is_error")
    if (
        type(is_error) is not bool
        or not isinstance(subtype, str)
        or subtype
        not in {
            "success",
            "error_during_execution",
            "error_max_turns",
            "error_max_budget_usd",
            "error_max_structured_output_retries",
        }
    ):
        raise ClaudeStreamError("claude returned an unknown terminal outcome")
    if (subtype == "success" and is_error) or (subtype != "success" and not is_error):
        raise ClaudeStreamError("claude returned a conflicting terminal outcome")
    if is_error:
        code = _terminal_failure_code(terminal, assistant_error)
        message = (
            "Claude reported a terminal quota rejection; reset time is unknown."
            if code == "claude_quota_exhausted"
            else "Claude reported a terminal provider failure."
        )
        raise ClaudeTerminalFailure(code, message, session_id)
    if returncode != 0:
        raise ClaudeStreamError("claude process exit conflicts with its successful result")
    text = terminal.get("result")
    if not isinstance(text, str) or not text.strip():
        raise ClaudeStreamError("claude completed without visible text")
    if len(text) > MAX_CLAUDE_VISIBLE_CHARACTERS:
        raise ClaudeStreamError("claude visible result exceeded its limit")
    return ClaudeParsedResult(
        text.strip(), session_id, answered_model or initialized_model or requested_model
    )
