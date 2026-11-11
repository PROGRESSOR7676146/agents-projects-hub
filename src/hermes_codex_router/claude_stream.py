from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .claude_quota_observation import USAGE_LIMIT_TYPES, QuotaObservationCallback, QuotaObserver
from .claude_usage import ClaudeUsageObserver, UsageSummaryCallback
from .diagnostic_log import survived

if TYPE_CHECKING:
    from .claude_corrections import ClaudeCorrectionInput
    from .claude_image_receipt import ClaudeImageReceipt

MAX_CLAUDE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CLAUDE_VISIBLE_CHARACTERS = 200_000
MAX_CLAUDE_EVENTS = 512
MAX_CLAUDE_STDERR_BYTES = 64 * 1024
MAX_CLAUDE_FILE_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_CLAUDE_FILE_EVENT_BYTES = 4 * 1024 * 1024
MAX_CLAUDE_FILE_EVENTS = 4096
MAX_IMAGE_EVENT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_STREAM_BYTES = 12 * 1024 * 1024


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


def _event(
    line: str, policy: Callable[[dict[str, object]], None] | None = None
) -> dict[str, object]:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError) as exc:
        raise ClaudeStreamError("claude returned malformed structured output") from exc
    if not isinstance(event, dict):
        raise ClaudeStreamError("claude returned a non-object event")
    (policy or _require_text_only_event)(event)
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
        on_quota_observation: QuotaObservationCallback | None = None,
        event_policy: Callable[[dict[str, object]], None] | None = None,
        image_receipt: ClaudeImageReceipt | None = None,
        correction_input: ClaudeCorrectionInput | None = None,
        on_usage_summary: UsageSummaryCallback | None = None,
    ) -> None:
        self.session_id = expected_session_id
        self.on_visible_assistant = on_visible_assistant
        self._usage = ClaudeUsageObserver() if on_usage_summary is not None else None
        self._on_usage_summary = on_usage_summary
        self._quota = (
            QuotaObserver(on_quota_observation, _terminal_failure_code)
            if on_quota_observation is not None
            else None
        )
        self.event_policy = event_policy
        if event_policy is not None and image_receipt is not None:
            raise ClaudeStreamError("claude image input cannot use file-tool policy")
        self.image_receipt = image_receipt
        if image_receipt is not None and correction_input is not None:
            raise ClaudeStreamError("claude corrections cannot use image input")
        self.correction_input = correction_input
        self._output = bytearray()
        self._pending = bytearray()
        self._events = 0
        self._terminal_seen = False
        self._visible: dict[str, str] = {}
        self._visible_characters = 0
        self._received_bytes = 0

    @property
    def terminal_seen(self) -> bool:
        return self._terminal_seen

    def publish_quota_observations(
        self, completed: subprocess.CompletedProcess[str] | None = None, interrupted: bool = False
    ) -> None:
        """Validate retained output after the drain, then publish best-effort evidence."""
        if self._quota is None or not self._quota.has_observations:
            return
        validated = False
        if completed is not None and completed.returncode >= 0 and not interrupted:
            try:
                # The incremental reader already checked the policy on every raw
                # event; retained file-tool output has deliberately omitted fields.
                parse_claude_stream(
                    completed.stdout,
                    expected_session_id=self.session_id,
                    returncode=completed.returncode,
                    event_policy=(lambda _: None) if self.event_policy else None,
                )
            except ClaudeTerminalFailure:
                validated = True
            except ClaudeStreamError:
                pass
            except Exception as error:
                survived("claude_stream.quota_observation", error)
            else:
                validated = True
        self._quota.publish(stream_validated=validated)

    def feed(self, chunk: bytes) -> None:
        self._received_bytes += len(chunk)
        byte_limit = (
            MAX_IMAGE_STREAM_BYTES
            if self.image_receipt
            else MAX_CLAUDE_FILE_OUTPUT_BYTES
            if self.event_policy
            else MAX_CLAUDE_OUTPUT_BYTES
        )
        if self._received_bytes > byte_limit:
            raise ClaudeStreamError("claude structured output exceeded its limit")
        if (
            self.event_policy is None
            and self.image_receipt is None
            and self.correction_input is None
        ):
            self._output.extend(chunk)
        self._pending.extend(chunk)
        while (end := self._pending.find(b"\n")) >= 0:
            line = bytes(self._pending[:end])
            del self._pending[: end + 1]
            self._line(line)
        if self.event_policy and len(self._pending) > MAX_CLAUDE_FILE_EVENT_BYTES:
            raise ClaudeStreamError("claude structured event exceeded its limit")
        if self.image_receipt and len(self._pending) > MAX_IMAGE_EVENT_BYTES:
            raise ClaudeStreamError("claude structured event exceeded its limit")

    def finish(self) -> str:
        if self._pending:
            self._line(bytes(self._pending))
            self._pending.clear()
        if self.image_receipt is not None:
            self.image_receipt.finish()
        if self.correction_input is not None:
            self.correction_input.finish()
        try:
            output = self._output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClaudeStreamError("claude returned invalid structured encoding") from exc
        self._publish_usage()
        return output

    def _publish_usage(self) -> None:
        # Telemetry is optional: it must never change the turn's outcome.
        if self._usage is None or self._on_usage_summary is None:
            return
        try:
            summary = self._usage.summary()
            if summary is not None:
                self._on_usage_summary(summary)
        except Exception as error:
            survived("claude_stream.usage_summary", error)

    def validate_interrupted_tail(self) -> None:
        """Validate a complete final JSON event even without its line delimiter.

        Only an incomplete JSON fragment may be discarded by a proven local
        kill. A complete event still owns session, policy and terminal checks.
        ``finish`` remains strict for every non-interrupted outcome.
        """
        if not self._pending:
            return
        try:
            line = bytes(self._pending).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClaudeStreamError("claude returned invalid structured encoding") from exc
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            if exc.pos >= len(line.rstrip()) or exc.msg.startswith("Unterminated string"):
                return
            raise ClaudeStreamError("claude returned malformed structured output") from exc
        self._line(bytes(self._pending))
        self._pending.clear()

    def _line(self, raw: bytes) -> None:
        if self.image_receipt and len(raw) > MAX_IMAGE_EVENT_BYTES:
            raise ClaudeStreamError("claude structured event exceeded its limit")
        if self.event_policy and len(raw) > MAX_CLAUDE_FILE_EVENT_BYTES:
            raise ClaudeStreamError("claude structured event exceeded its limit")
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClaudeStreamError("claude returned invalid structured encoding") from exc
        if not line.strip():
            return
        event = _event(line, self.event_policy)
        self._events += 1
        if self._events > (MAX_CLAUDE_FILE_EVENTS if self.event_policy else MAX_CLAUDE_EVENTS):
            raise ClaudeStreamError("claude structured output exceeded its event limit")
        if "session_id" in event:
            identity = _session_id(event["session_id"])
            if self.session_id is not None and self.session_id != identity:
                raise ClaudeStreamError("claude stream session identity changed")
            self.session_id = identity
        if event.get("type") == "result":
            _validate_terminal(event)
        if self._quota is not None and not self._terminal_seen:
            self._quota.observe(event, self.session_id)
        if self._usage is not None and not self._terminal_seen:
            self._usage.observe(event)
        retain = (
            self.correction_input.observe(event)
            if self.correction_input
            else self.image_receipt.observe(event)
            if self.image_receipt
            else True
        )
        if (
            self._terminal_seen
            and event.get("type") != "prompt_suggestion"
            and not (not retain and event.get("type") == "command_lifecycle")
        ):
            raise ClaudeStreamError("claude returned events after its terminal outcome")
        if event.get("type") == "result" and retain:
            self._terminal_seen = True
        if not retain:
            return
        if self.image_receipt is not None:
            if len(self._output) + len(raw) + 1 > MAX_CLAUDE_OUTPUT_BYTES:
                raise ClaudeStreamError("claude retained visible output exceeded its limit")
            self._output.extend(raw + b"\n")
        if self.on_visible_assistant is not None:
            self._visible_assistant(event)
        if self.event_policy is not None or self.correction_input is not None:
            retained = _file_result_evidence(event)
            if retained is not None:
                encoded = (json.dumps(retained, ensure_ascii=False) + "\n").encode("utf-8")
                if len(self._output) + len(encoded) > MAX_CLAUDE_OUTPUT_BYTES:
                    raise ClaudeStreamError("claude retained visible output exceeded its limit")
                self._output.extend(encoded)

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


def _file_result_evidence(event: dict[str, object]) -> dict[str, object] | None:
    """Retain terminal/model/text evidence after validating every raw event."""
    if event.get("type") not in {"assistant", "result", "prompt_suggestion"} and not (
        event.get("type") == "system" and event.get("subtype") == "init"
    ):
        return None
    retained = {
        key: event[key]
        for key in (
            "type",
            "subtype",
            "session_id",
            "uuid",
            "parent_tool_use_id",
            "error",
            "aborted",
            "model",
            "result",
            "is_error",
            "api_error_status",
        )
        if key in event
    }
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        retained["message"] = {
            "model": message.get("model"),
            "content": [
                {"type": "text", "text": block.get("text")}
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if isinstance(content, list)
            else [],
        }

    return retained


def _session_id(value: object) -> str:
    if not isinstance(value, str):
        raise ClaudeStreamError("claude returned no session id")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise ClaudeStreamError("claude returned an invalid session id") from exc
    return value


def _terminal_failure_code(
    terminal: dict[str, object], assistant_error: str | None, *, quota_rejected: bool = False
) -> str:
    # A 429 alone does not say which limit applied: short-term rate limits,
    # proxy credential cooldowns and the account usage limit all return it.
    # Only the native rejected rate-limit event identifies the usage limit.
    rate_limit = "claude_quota_exhausted" if quota_rejected else "claude_rate_limited"
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
        return rate_limit
    if type(status) is int and status == 401:
        return "claude_authentication_failed"
    if type(status) is int and status == 529:
        return "claude_provider_overloaded"
    if type(status) is int and status in {500, 502, 503, 504}:
        # Reached only after the CLI's own retries: a network, DNS or upstream outage.
        return "claude_provider_unavailable"
    if subtype == "success":
        # The reinterpreted native HTTP failure is proven by the exact status;
        # assistant text must not turn another 4xx/5xx into quota evidence.
        return "claude_provider_failure"
    error_codes = {
        "rate_limit": rate_limit,
        "authentication_failed": "claude_authentication_failed",
        "billing_error": "claude_billing_error",
        "overloaded": "claude_provider_overloaded",
        "model_not_found": "claude_model_not_found",
    }
    return error_codes.get(assistant_error or "", "claude_provider_failure")


def _usage_limit_rejected(event: dict[str, object]) -> bool:
    """Return whether a native rate-limit event rejected the request on a named limit."""
    if event.get("type") != "rate_limit_event":
        return False
    info = event.get("rate_limit_info")
    fields = info if isinstance(info, dict) else event
    return fields.get("status") == "rejected" and fields.get("rateLimitType") in USAGE_LIMIT_TYPES


def _terminal_failure_message(code: str) -> str:
    inspect_work = "Inspect saved partial work before sending a new request."
    inspect_limits = (
        "Inspect saved partial work and the configured execution limits before continuing."
    )
    check_route = "Check the configured Claude/CPA route locally before sending new work."
    reason, action = {
        "claude_quota_exhausted": (
            "Claude's account usage limit was reached; the route reported no reset time, "
            "so reset time is unknown.",
            "The conversation and saved partial work are preserved; send the request again "
            "after the limit resets.",
        ),
        "claude_rate_limited": (
            "Claude rejected this request with HTTP 429. The route did not identify the limit "
            "or its reset time.",
            "The conversation and any saved partial work are preserved; you can send a short "
            "follow-up such as “continue” later.",
        ),
        "claude_authentication_failed": ("Claude reported an authentication failure.", check_route),
        "claude_billing_error": ("Claude reported a billing rejection.", check_route),
        "claude_model_not_found": (
            "Claude reported that the selected model was not found.",
            "Review the configured model selection before sending new work.",
        ),
        "claude_provider_overloaded": (
            "Claude reported temporary provider overload.",
            inspect_work,
        ),
        "claude_provider_unavailable": (
            "Claude could not reach its API after retrying "
            "(a temporary network, DNS or upstream outage).",
            "The conversation and saved partial work are preserved; once the connection is "
            "back, send a short follow-up such as “continue” to resume.",
        ),
        "claude_turn_limit": ("Claude reached the configured turn limit.", inspect_limits),
        "claude_budget_exhausted": (
            "Claude reached the configured execution budget.",
            inspect_limits,
        ),
        "claude_structured_output_failed": (
            "Claude exhausted its structured-output attempts.",
            "Inspect saved partial work and the requested output format before continuing.",
        ),
    }.get(code, ("Claude reported a terminal provider failure.", inspect_work))
    return f"{reason} No completed result was returned. {action}"


def _validate_terminal(terminal: dict[str, object]) -> bool:
    """Check complete-event contradictions before a stop can discard the stream."""
    _session_id(terminal.get("session_id"))
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
    # Native CLI uses the success result variant for a completed API-error
    # message too. Accept only its explicit HTTP error/status and result shape
    # here, always as failure; an absent/ambiguous status stays indeterminate.
    status = terminal.get("api_error_status")
    http_error_status = type(status) is int and 400 <= status <= 599
    native_http_failure = (
        subtype == "success"
        and is_error
        and http_error_status
        and isinstance(terminal.get("result"), str)
    )
    if (
        (subtype == "success" and is_error and not native_http_failure)
        or (subtype == "success" and not is_error and status is not None)
        or (subtype != "success" and not is_error)
    ):
        raise ClaudeStreamError("claude returned a conflicting terminal outcome")
    if not is_error:
        text = terminal.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ClaudeStreamError("claude completed without visible text")
        if len(text) > MAX_CLAUDE_VISIBLE_CHARACTERS:
            raise ClaudeStreamError("claude visible result exceeded its limit")
    return is_error


def parse_claude_stream(
    output: str,
    *,
    expected_session_id: str | None = None,
    requested_model: str | None = None,
    returncode: int = 0,
    event_policy: Callable[[dict[str, object]], None] | None = None,
) -> ClaudeParsedResult:
    """Validate the entire bounded NDJSON stream before accepting its result.

    Earlier errors cannot override a successful terminal result. A conflicting
    later event invalidates even an otherwise well-formed terminal outcome.
    """
    byte_limit = MAX_CLAUDE_FILE_OUTPUT_BYTES if event_policy else MAX_CLAUDE_OUTPUT_BYTES
    if len(output) > byte_limit or len(output.encode("utf-8")) > byte_limit:
        raise ClaudeStreamError("claude structured output exceeded its limit")
    events: list[dict[str, object]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        events.append(_event(line, event_policy))
        if len(events) > (MAX_CLAUDE_FILE_EVENTS if event_policy else MAX_CLAUDE_EVENTS):
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
    quota_rejected = False
    for event in events:
        if "session_id" in event and _session_id(event["session_id"]) != session_id:
            raise ClaudeStreamError("claude stream session identity changed")
        quota_rejected = quota_rejected or _usage_limit_rejected(event)
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
    is_error = _validate_terminal(terminal)
    if is_error:
        code = _terminal_failure_code(terminal, assistant_error, quota_rejected=quota_rejected)
        raise ClaudeTerminalFailure(code, _terminal_failure_message(code), session_id)
    if returncode != 0:
        raise ClaudeStreamError("claude process exit conflicts with its successful result")
    text = terminal.get("result")
    assert isinstance(text, str)
    return ClaudeParsedResult(
        text.strip(), session_id, answered_model or initialized_model or requested_model
    )
