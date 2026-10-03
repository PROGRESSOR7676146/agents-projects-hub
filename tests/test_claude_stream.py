from __future__ import annotations

import json
import unittest
from collections.abc import Mapping

from hermes_codex_router.claude_stream import (
    MAX_CLAUDE_OUTPUT_BYTES,
    MAX_CLAUDE_VISIBLE_CHARACTERS,
    ClaudeStreamError,
    ClaudeStreamReader,
    ClaudeTerminalFailure,
    ClaudeVisibleAssistant,
    parse_claude_stream,
)

SESSION = "00000000-0000-4000-8000-000000000001"
OTHER_SESSION = "019abcde-1234-7fff-8fff-0123456789ab"


def output(*events: Mapping[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def result(**changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": SESSION,
        "result": "Visible answer",
    }
    event.update(changes)
    return event


class ClaudeStreamTests(unittest.TestCase):
    def test_incremental_callback_has_only_native_id_and_visible_main_text(self) -> None:
        items: list[ClaudeVisibleAssistant] = []
        reader = ClaudeStreamReader(expected_session_id=SESSION, on_visible_assistant=items.append)
        message_id = OTHER_SESSION
        event = {
            "type": "assistant",
            "session_id": SESSION,
            "uuid": message_id,
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private thought"},
                    {"type": "text", "text": "Visible é"},
                ]
            },
        }
        chunk = (output(event, event) + "\n").encode()
        for byte in chunk:
            reader.feed(bytes([byte]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].message_id, message_id)
        self.assertEqual(items[0].session_id, SESSION)
        self.assertEqual(items[0].text, "Visible é")
        self.assertNotIn("private", repr(items))
        reader.feed(output(result()).encode())
        self.assertEqual(parse_claude_stream(reader.finish()).text, "Visible answer")

    def test_incremental_callback_rejects_missing_or_changed_native_identity(self) -> None:
        base: dict[str, object] = {
            "type": "assistant",
            "session_id": SESSION,
            "parent_tool_use_id": None,
            "message": {"content": [{"type": "text", "text": "Visible"}]},
        }
        missing_parent = {key: value for key, value in base.items() if key != "parent_tool_use_id"}
        reader = ClaudeStreamReader(on_visible_assistant=lambda _: None)
        with self.assertRaisesRegex(ClaudeStreamError, "ownership is missing"):
            reader.feed((output({**missing_parent, "uuid": SESSION}) + "\n").encode())
        for change in (
            {},
            {"uuid": "invalid"},
            {"uuid": SESSION, "session_id": OTHER_SESSION},
            {"uuid": SESSION, "parent_tool_use_id": 7},
        ):
            reader = ClaudeStreamReader(
                expected_session_id=SESSION, on_visible_assistant=lambda _: None
            )
            with self.subTest(change=change), self.assertRaises(ClaudeStreamError):
                reader.feed((output({**base, **change}) + "\n").encode())
        reader = ClaudeStreamReader(
            expected_session_id=SESSION, on_visible_assistant=lambda _: None
        )
        reader.feed((output({**base, "uuid": SESSION}) + "\n").encode())
        with self.assertRaises(ClaudeStreamError):
            reader.feed(
                (
                    output(
                        {
                            **base,
                            "uuid": SESSION,
                            "message": {"content": [{"type": "text", "text": "Changed"}]},
                        }
                    )
                    + "\n"
                ).encode()
            )

    def test_incremental_callback_skips_errors_aborts_and_partial_events(self) -> None:
        items: list[ClaudeVisibleAssistant] = []
        reader = ClaudeStreamReader(expected_session_id=SESSION, on_visible_assistant=items.append)
        base = {
            "type": "assistant",
            "session_id": SESSION,
            "uuid": SESSION,
            "parent_tool_use_id": None,
            "message": {"content": [{"type": "text", "text": "private synthetic"}]},
        }
        for change in (
            {"error": "rate_limit"},
            {"aborted": True},
            {"type": "stream_event"},
        ):
            reader.feed((output({**base, **change}) + "\n").encode())
        self.assertEqual(items, [])

    def test_text_only_policy_rejects_capabilities_and_actions_before_visible_publication(
        self,
    ) -> None:
        unsafe = (
            {"type": "system", "subtype": "init", "tools": ["Bash"]},
            {"type": "system", "subtype": "init", "tools": ""},
            {"type": "system", "subtype": "init", "mcp_servers": [{"name": "private"}]},
            {"type": "system", "subtype": "init", "plugins": [{"name": "private"}]},
            {"type": "system", "subtype": "init", "skills": ["private"]},
            {"type": "system", "subtype": "init", "permissionMode": "manual"},
            {"type": "control_request", "request": {"subtype": "can_use_tool", "input": "private"}},
            {"type": "system", "subtype": "hook_started", "hook_name": "private"},
            {"type": "system", "subtype": "task_started", "description": "private"},
            {"type": "tool_progress", "tool_name": "private"},
            {"type": "tool_use_summary", "summary": "private"},
            {
                "type": "assistant",
                "uuid": OTHER_SESSION,
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {"type": "text", "text": "Must not be published"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "private"}},
                    ]
                },
            },
            {"type": "assistant", "parent_tool_use_id": "private", "message": {"content": []}},
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "private"}]},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {"type": "server_tool_use", "input": "private"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "input_json_delta", "partial_json": "private"},
                },
            },
        )
        for event in unsafe:
            event = {"session_id": SESSION, **event}
            with self.subTest(event=event):
                visible: list[ClaudeVisibleAssistant] = []
                for callback in (None, visible.append):
                    reader = ClaudeStreamReader(
                        expected_session_id=SESSION, on_visible_assistant=callback
                    )
                    with self.assertRaisesRegex(ClaudeStreamError, "text-only") as raised:
                        reader.feed((output(event) + "\n").encode())
                    self.assertNotIn("private", str(raised.exception))
                self.assertEqual(visible, [])
                with self.assertRaisesRegex(ClaudeStreamError, "text-only"):
                    parse_claude_stream(output(event, result()), expected_session_id=SESSION)

    def test_text_only_initialization_and_partial_text_preserve_terminal_result(self) -> None:
        stream = output(
            {
                "type": "system",
                "subtype": "init",
                "session_id": SESSION,
                "tools": [],
                "mcp_servers": [],
                "plugins": [],
                "skills": [],
                "permissionMode": "dontAsk",
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "provisional"},
                },
            },
            result(),
        )
        reader = ClaudeStreamReader(expected_session_id=SESSION)
        reader.feed(stream.encode())
        self.assertEqual(parse_claude_stream(reader.finish()).text, "Visible answer")

    def test_incremental_callback_failure_is_uncertain_without_exception_detail(self) -> None:
        def fail(_: object) -> None:
            raise RuntimeError("private persistence diagnosis")

        reader = ClaudeStreamReader(expected_session_id=SESSION, on_visible_assistant=fail)
        with self.assertRaises(ClaudeStreamError) as raised:
            reader.feed(
                (
                    output(
                        {
                            "type": "assistant",
                            "uuid": SESSION,
                            "session_id": SESSION,
                            "parent_tool_use_id": None,
                            "message": {"content": [{"type": "text", "text": "Visible"}]},
                        }
                    )
                    + "\n"
                ).encode()
            )
        self.assertNotIn("private persistence diagnosis", str(raised.exception))

    def test_incremental_reader_bounds_pending_bytes_and_events_without_eof(self) -> None:
        reader = ClaudeStreamReader(expected_session_id=SESSION)
        with self.assertRaisesRegex(ClaudeStreamError, "output.*limit"):
            for _ in range(33):
                reader.feed(b"x" * 65536)
        reader = ClaudeStreamReader(expected_session_id=SESSION)
        with self.assertRaisesRegex(ClaudeStreamError, "event limit"):
            for _ in range(513):
                reader.feed(b'{"type":"system"}\n')
        with self.assertRaisesRegex(ClaudeStreamError, "encoding"):
            ClaudeStreamReader().feed(b"\xff\n")

    def test_incremental_reader_retains_partial_before_later_protocol_failure(self) -> None:
        visible: list[ClaudeVisibleAssistant] = []
        reader = ClaudeStreamReader(
            expected_session_id=SESSION, on_visible_assistant=visible.append
        )
        reader.feed(
            (
                output(
                    {
                        "type": "assistant",
                        "uuid": OTHER_SESSION,
                        "session_id": SESSION,
                        "parent_tool_use_id": None,
                        "message": {"content": [{"type": "text", "text": "Saved incomplete"}]},
                    }
                )
                + "\n"
            ).encode()
        )
        with self.assertRaises(ClaudeStreamError):
            reader.feed((output(result(session_id=OTHER_SESSION)) + "\n").encode())
        self.assertEqual([item.text for item in visible], ["Saved incomplete"])

    def test_returns_only_terminal_visible_text_and_answered_model(self) -> None:
        parsed = parse_claude_stream(
            output(
                {"type": "system", "subtype": "init", "session_id": SESSION, "model": "init"},
                {
                    "type": "assistant",
                    "session_id": SESSION,
                    "message": {
                        "model": "answered",
                        "content": [{"type": "thinking", "thinking": "private"}],
                    },
                },
                result(),
            ),
            expected_session_id=SESSION,
            requested_model="requested",
        )
        self.assertEqual(
            (parsed.text, parsed.session_id, parsed.model), ("Visible answer", SESSION, "answered")
        )
        self.assertNotIn("private", repr(parsed))

    def test_verified_terminal_quota_has_safe_typed_metadata_without_reset(self) -> None:
        for events in (
            (
                result(
                    subtype="error_during_execution",
                    is_error=True,
                    api_error_status=429,
                    errors=["secret diagnosis"],
                ),
            ),
            (
                {"type": "assistant", "session_id": SESSION, "error": "rate_limit"},
                result(
                    subtype="error_during_execution", is_error=True, errors=["secret diagnosis"]
                ),
            ),
        ):
            with self.subTest(events=events), self.assertRaises(ClaudeTerminalFailure) as raised:
                parse_claude_stream(output(*events), expected_session_id=SESSION)
            self.assertEqual(raised.exception.code, "claude_quota_exhausted")
            self.assertEqual(raised.exception.session_id, SESSION)
            self.assertNotIn("secret diagnosis", str(raised.exception))
            self.assertFalse(hasattr(raised.exception, "resets_at"))

    def test_success_after_transient_quota_is_success(self) -> None:
        parsed = parse_claude_stream(
            output({"type": "assistant", "session_id": SESSION, "error": "rate_limit"}, result()),
            expected_session_id=SESSION,
        )
        self.assertEqual(parsed.text, "Visible answer")

    def test_unbound_or_superseded_assistant_error_is_not_quota_evidence(self) -> None:
        sequences = (
            ({"type": "assistant", "error": "rate_limit"},),
            (
                {"type": "assistant", "session_id": SESSION, "error": "rate_limit"},
                {"type": "assistant", "session_id": SESSION, "message": {"content": []}},
            ),
        )
        for preceding in sequences:
            with (
                self.subTest(preceding=preceding),
                self.assertRaises(ClaudeTerminalFailure) as raised,
            ):
                parse_claude_stream(
                    output(*preceding, result(subtype="error_during_execution", is_error=True)),
                    expected_session_id=SESSION,
                )
            self.assertEqual(raised.exception.code, "claude_provider_failure")

    def test_terminal_error_subtypes_have_bounded_public_codes(self) -> None:
        for subtype, code in (
            ("error_during_execution", "claude_provider_failure"),
            ("error_max_turns", "claude_turn_limit"),
            ("error_max_budget_usd", "claude_budget_exhausted"),
            ("error_max_structured_output_retries", "claude_structured_output_failed"),
        ):
            with self.subTest(subtype=subtype), self.assertRaises(ClaudeTerminalFailure) as raised:
                parse_claude_stream(
                    output(result(subtype=subtype, is_error=True)), expected_session_id=SESSION
                )
            self.assertEqual(raised.exception.code, code)

    def test_protocol_ambiguity_never_becomes_terminal_failure(self) -> None:
        ambiguous = (
            "",
            '{"type":"result", broken',
            "[]",
            output({"type": "assistant", "session_id": SESSION}),
            output(result(), result()),
            output(result(), result(is_error=True, api_error_status=429)),
            output(result(session_id=OTHER_SESSION)),
            output({"type": "system", "subtype": "init", "session_id": OTHER_SESSION}, result()),
            output(result(), {"type": "assistant", "session_id": OTHER_SESSION}),
            output(result(), {"type": "assistant", "session_id": SESSION}),
            output(result(session_id="invalid")),
            output(result(subtype="unknown", is_error=True)),
            output(result(subtype={}, is_error=True)),
            output(result(subtype="error_during_execution", is_error=False)),
            output(result(is_error="false")),
            output(result(subtype="success", is_error=True, api_error_status=429)),
        )
        for stream in ambiguous:
            with self.subTest(stream=stream), self.assertRaises(ClaudeStreamError):
                parse_claude_stream(stream, expected_session_id=SESSION)

    def test_prompt_suggestion_after_result_is_not_another_turn(self) -> None:
        parsed = parse_claude_stream(
            output(result(), {"type": "prompt_suggestion", "session_id": SESSION}),
            expected_session_id=SESSION,
        )
        self.assertEqual(parsed.text, "Visible answer")

    def test_nonzero_exit_cannot_convert_valid_success_to_success(self) -> None:
        with self.assertRaises(ClaudeStreamError):
            parse_claude_stream(output(result()), expected_session_id=SESSION, returncode=1)

    def test_nonzero_exit_still_recognizes_verified_terminal_rejection(self) -> None:
        with self.assertRaises(ClaudeTerminalFailure) as raised:
            parse_claude_stream(
                output(
                    result(subtype="error_during_execution", is_error=True, api_error_status=429)
                ),
                expected_session_id=SESSION,
                returncode=1,
            )
        self.assertEqual(raised.exception.code, "claude_quota_exhausted")

    def test_output_and_visible_text_are_bounded(self) -> None:
        for stream in (
            " " * (MAX_CLAUDE_OUTPUT_BYTES + 1),
            output(result(result="x" * (MAX_CLAUDE_VISIBLE_CHARACTERS + 1))),
            output(*({"type": "system"} for _ in range(513)), result()),
            "é" * (MAX_CLAUDE_OUTPUT_BYTES // 2 + 1),
        ):
            with self.subTest(size=len(stream)), self.assertRaises(ClaudeStreamError):
                parse_claude_stream(stream, expected_session_id=SESSION)


if __name__ == "__main__":
    unittest.main()
