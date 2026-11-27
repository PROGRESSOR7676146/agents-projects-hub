"""Reduced native Claude Code 2.1.332 streams with a fictional upstream."""

from __future__ import annotations

import json
import unittest
import uuid

from hermes_codex_router.claude_corrections import ClaudeCorrectionCallbacks, ClaudeCorrectionInput
from hermes_codex_router.claude_host_profile import host_event_policy
from hermes_codex_router.claude_stream import (
    ClaudeStreamError,
    ClaudeStreamReader,
    ClaudeTerminalFailure,
    parse_claude_stream,
)
from tests.claude_image_request_contract import IMAGE_BASE64

SESSION = "00000000-0000-4000-8000-000000000001"
FOREIGN = str(uuid.UUID(int=2, version=4))


def native_event(kind: str, **fields: object) -> dict:
    return {"type": kind, "session_id": SESSION, "uuid": str(uuid.uuid4()), **fields}


def permission_denial(input_uuid: str) -> dict:
    return native_event(
        "result",
        subtype="success",
        is_error=True,
        api_error_status=403,
        api_error_type="permission_error",
        terminal_reason="api_error",
        user_message_uuid=input_uuid,
        user_message_uuids=[input_uuid],
        result="Example organization denied Claude Code access.",
    )


def image_events() -> tuple[dict, dict, dict]:
    tool = "toolu_example_read"
    read = native_event(
        "assistant",
        parent_tool_use_id=None,
        message={
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "id": tool,
                    "input": {"file_path": "example.png"},
                }
            ],
        },
    )
    image = native_event(
        "user",
        parent_tool_use_id=None,
        message={
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool,
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "data": IMAGE_BASE64[0],
                                "media_type": "image/png",
                            },
                        }
                    ],
                }
            ],
        },
        tool_use_result={
            "type": "image",
            "file": {
                "dimensions": {
                    "originalWidth": 3000,
                    "originalHeight": 2000,
                    "displayWidth": 2000,
                    "displayHeight": 1333,
                }
            },
        },
    )
    note = native_event(
        "user",
        parent_tool_use_id=None,
        isSynthetic=True,
        timestamp="2026-01-01T00:00:00Z",
        message={
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[Image: original 3000x2000, displayed at 2000x1333. "
                    "Multiply coordinates by 1.50 to map to original image.]",
                }
            ],
        },
    )
    return read, image, note


class ClaudeStreamStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipts: list[str] = []
        self.channel = ClaudeCorrectionInput(
            "Example prompt",
            SESSION,
            ClaudeCorrectionCallbacks(
                prepare=lambda _: None,
                claim=lambda: None,
                receipt=lambda identifier, _: self.receipts.append(identifier),
                result=lambda _: None,
            ),
        )
        self.identifier = self.channel.initial.uuid
        self.reader = ClaudeStreamReader(
            expected_session_id=SESSION,
            correction_input=self.channel,
            event_policy=host_event_policy,
        )

    def lifecycle(self, state: str, **fields: object) -> dict:
        return native_event(
            "command_lifecycle",
            **{
                "state": state,
                "command_uuid": self.identifier,
                **fields,
            },
        )

    def feed(self, *events: dict) -> None:
        for event in events:
            self.reader.feed((json.dumps(event) + "\n").encode())

    def replay(self) -> dict:
        return {**json.loads(self.channel.initial_data), "isReplay": True}

    def begin(self) -> None:
        self.feed(self.lifecycle("queued"), self.lifecycle("started"), self.replay())

    def finish(self, *, returncode: int = 0) -> str:
        return parse_claude_stream(
            self.reader.finish(),
            expected_session_id=SESSION,
            event_policy=host_event_policy,
            returncode=returncode,
        ).text

    def test_exact_access_denial_accepts_cancel_before_or_after_result(self) -> None:
        for cancel_first in (True, False):
            with self.subTest(cancel_first=cancel_first):
                self.setUp()
                self.begin()
                self.feed(
                    native_event(
                        "assistant",
                        parent_tool_use_id=None,
                        is_api_error_message=True,
                        api_error_status=403,
                        api_error_type="permission_error",
                        error="authentication_failed",
                        user_message_uuid=self.identifier,
                        user_message_uuids=[self.identifier],
                        message={
                            "model": "<synthetic>",
                            "content": [{"type": "text", "text": "Example typed error"}],
                        },
                    )
                )
                ordered = [self.lifecycle("cancelled"), permission_denial(self.identifier)]
                self.feed(*(ordered if cancel_first else reversed(ordered)))
                with self.assertRaises(ClaudeTerminalFailure) as failure:
                    self.finish(returncode=1)
                self.assertEqual(failure.exception.code, "claude_provider_access_denied")
                self.assertIn("HTTP 403", failure.exception.public_message)
                self.assertNotIn("Example", failure.exception.public_message)
                self.assertEqual(self.receipts, [self.identifier])

    def test_cancellation_requires_confirmed_single_input_and_exact_denial_without_work(
        self,
    ) -> None:
        for mismatch in (
            "no_result",
            "success",
            "foreign_result",
            "foreign_result_event",
            "result_uuid_reused",
            "foreign_session",
            "foreign_command",
            "foreign_owner",
            "duplicate",
            "missing_receipt",
            "duplicate_uuid",
            "bad_uuid",
            "earlier_work",
            "earlier_hook",
            "wrong_reason",
            "non_permission_403",
            "pending_correction",
        ):
            with self.subTest(mismatch=mismatch):
                self.setUp()
                queued = self.lifecycle("queued")
                self.feed(queued, self.lifecycle("started"))
                if mismatch != "missing_receipt":
                    self.feed(self.replay())
                if mismatch == "earlier_work":
                    self.feed(image_events()[0])
                if mismatch == "earlier_hook":
                    self.feed(native_event("system", subtype="hook_started"))
                if mismatch == "pending_correction":
                    from hermes_codex_router.claude_corrections import ClaudeInputFrame

                    self.channel.callbacks.claim = lambda: ClaudeInputFrame(
                        FOREIGN, SESSION, "Example correction"
                    )
                    self.assertIsNotNone(self.channel.poll())
                cancel = self.lifecycle(
                    "cancelled",
                    **{
                        "foreign_session": {"session_id": FOREIGN},
                        "foreign_command": {"command_uuid": FOREIGN},
                        "duplicate_uuid": {"uuid": queued["uuid"]},
                        "bad_uuid": {"uuid": "not-a-uuid"},
                    }.get(mismatch, {}),
                )
                result = permission_denial(self.identifier)
                if mismatch == "foreign_result_event":
                    result["uuid"] = self.identifier
                if mismatch == "result_uuid_reused":
                    result["uuid"] = cancel["uuid"]
                if mismatch == "foreign_result":
                    result["user_message_uuid"] = FOREIGN
                if mismatch == "foreign_owner":
                    result["parent_tool_use_id"] = "toolu_foreign"
                if mismatch == "wrong_reason":
                    result["terminal_reason"] = "interrupted"
                if mismatch == "non_permission_403":
                    result["api_error_type"] = "unknown"
                if mismatch == "success":
                    result.update(is_error=False, api_error_status=None, result="Example success")
                with self.assertRaises(ClaudeStreamError):
                    self.feed(cancel)
                    if mismatch == "duplicate":
                        self.feed(self.lifecycle("cancelled"))
                    if mismatch != "no_result":
                        self.feed(result)
                    self.finish()

    def test_read_image_metadata_does_not_replace_receipt_or_terminal_result(self) -> None:
        self.begin()
        self.feed(*image_events())
        self.assertEqual(self.receipts, [self.identifier])
        self.assertFalse(self.reader.terminal_seen)
        with self.assertRaises(ClaudeStreamError):
            self.reader.finish()

    def test_native_read_image_metadata_allows_exact_terminal_success(self) -> None:
        self.begin()
        self.feed(*image_events())
        self.feed(native_event("result", subtype="success", is_error=False, result="Example final"))
        self.feed(self.lifecycle("completed"))
        self.assertEqual(self.finish(), "Example final")
        self.assertEqual(self.receipts, [self.identifier])

    def test_synthetic_subagent_data_can_never_replace_main_input_or_result(self) -> None:
        self.begin()
        _, _, note = image_events()
        note["parent_tool_use_id"] = "toolu_example_child"
        self.feed(note)
        self.assertEqual(self.receipts, [self.identifier])
        self.assertFalse(self.reader.terminal_seen)
        self.feed(native_event("result", subtype="success", is_error=False, result="Example final"))
        self.assertEqual(self.finish(), "Example final")

    def test_foreign_synthetic_or_unbound_image_metadata_retains_uncertainty(self) -> None:
        for mismatch in (
            "missing_read",
            "missing_result",
            "wrong_tool",
            "foreign_owner",
            "foreign_session",
            "replay",
            "claimed_input_uuid",
            "wrong_scale",
            "duplicate",
            "later",
            "wrong_dimensions",
            "missing_receipt",
        ):
            with self.subTest(mismatch=mismatch):
                self.setUp()
                if mismatch != "missing_receipt":
                    self.begin()
                read, image, note = image_events()
                if mismatch == "wrong_tool":
                    read["message"]["content"][0]["name"] = "Other"
                if mismatch == "wrong_dimensions":
                    image["tool_use_result"]["file"]["dimensions"]["displayHeight"] = 1300
                if mismatch != "missing_read":
                    self.feed(read)
                if mismatch != "missing_result":
                    self.feed(image)
                note.update(
                    {
                        "foreign_owner": {"parent_tool_use_id": "toolu_foreign"},
                        "foreign_session": {"session_id": FOREIGN},
                        "replay": {"isReplay": True},
                        "claimed_input_uuid": {"uuid": self.identifier},
                    }.get(mismatch, {})
                )
                if mismatch == "wrong_scale":
                    note["message"]["content"][0]["text"] = "Example forged note"
                if mismatch == "later":
                    self.feed(native_event("system", subtype="example"))
                with self.assertRaises(ClaudeStreamError):
                    self.feed(note)
                    if mismatch == "duplicate":
                        self.feed(note)
