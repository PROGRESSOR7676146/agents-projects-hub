"""A native success cannot substitute for exact processed-input evidence."""

from __future__ import annotations

import base64
import json
import unittest
import uuid
from copy import deepcopy

from hermes_codex_router.claude_image_input import (
    MAX_CLAUDE_IMAGE_BYTES,
    encode_claude_image_input,
)
from hermes_codex_router.claude_image_receipt import ClaudeImageReceipt
from hermes_codex_router.claude_stream import ClaudeStreamError, ClaudeStreamReader
from tests.claude_image_request_contract import IMAGE_BASE64
from tests.test_claude_image_input import SESSION, image


class ClaudeImageReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = encode_claude_image_input("Example caption", (image(),), SESSION)
        self.frame = json.loads(self.data)
        self.ack = {**deepcopy(self.frame), "isReplay": True, "timestamp": "2026-01-01T00:00:00Z"}
        self.result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": SESSION,
            "result": "Example final",
        }

    def reader(self) -> ClaudeStreamReader:
        return ClaudeStreamReader(
            expected_session_id=SESSION,
            image_receipt=ClaudeImageReceipt(self.data, expected_session_id=SESSION),
        )

    def feed(self, reader: ClaudeStreamReader, events: list[dict]) -> str:
        raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode()
        for start in range(0, len(raw), 19):
            reader.feed(raw[start : start + 19])
        return reader.finish()

    def lifecycle(self, state: str) -> dict:
        return {
            "type": "command_lifecycle",
            "state": state,
            "session_id": SESSION,
            "command_uuid": self.frame["uuid"],
            "uuid": str(uuid.uuid4()),
        }

    def test_processed_receipt_is_required_and_image_data_is_not_retained(self) -> None:
        output = self.feed(
            self.reader(),
            [
                self.lifecycle("queued"),
                self.lifecycle("started"),
                self.ack,
                self.result,
                self.lifecycle("completed"),
            ],
        )
        self.assertEqual([json.loads(line)["type"] for line in output.splitlines()], ["result"])
        self.assertNotIn(self.frame["message"]["content"][2]["source"]["data"], output)
        with self.assertRaisesRegex(ClaudeStreamError, "image input"):
            self.feed(self.reader(), [self.result])

    def test_wrong_uuid_session_replay_or_duplicate_refuses(self) -> None:
        for change in (
            {"uuid": str(uuid.uuid4())},
            {"session_id": str(uuid.uuid4())},
            {"isReplay": False},
            {"parent_tool_use_id": "example-tool"},
        ):
            with self.subTest(change=change), self.assertRaises(ClaudeStreamError):
                self.feed(self.reader(), [{**self.ack, **change}, self.result])
        with self.assertRaises(ClaudeStreamError):
            self.feed(self.reader(), [self.ack, self.ack, self.result])

    def test_substituted_missing_or_changed_image_never_accepts_success(self) -> None:
        for replacement in (
            [{"type": "text", "text": "Example native image failure"}],
            list(reversed(self.frame["message"]["content"])),
            self.frame["message"]["content"][:-1],
        ):
            ack = deepcopy(self.ack)
            ack["message"]["content"] = replacement
            with self.subTest(replacement=len(replacement)), self.assertRaises(ClaudeStreamError):
                self.feed(self.reader(), [ack, self.result])
        for change in ({"media_type": "image/jpeg"}, {"data": "ZXhhbXBsZQ=="}):
            ack = deepcopy(self.ack)
            ack["message"]["content"][2]["source"].update(change)
            with self.subTest(change=change), self.assertRaises(ClaudeStreamError):
                self.feed(self.reader(), [ack, self.result])

    def test_closed_native_annotation_is_data_and_arbitrary_suffix_refuses(self) -> None:
        ack = deepcopy(self.ack)
        ack["message"]["content"].append(
            {
                "type": "text",
                "text": f"[Image: source: /tmp/claude-1000/-workspace-example/{SESSION}/images/1.png]",
            }
        )
        self.feed(self.reader(), [ack, self.result])
        ack["message"]["content"][-1]["text"] = "Example arbitrary native text"
        with self.assertRaises(ClaudeStreamError):
            self.feed(self.reader(), [ack, self.result])

    def test_correlated_native_png_to_jpeg_processing_preserves_text_and_slot(self) -> None:
        ack = deepcopy(self.ack)
        ack["message"]["content"][2]["source"].update(media_type="image/jpeg", data=IMAGE_BASE64[1])
        self.assertIn("Example final", self.feed(self.reader(), [ack, self.result]))
        ack["message"]["content"][1]["text"] = "MATERIAL 2 IMAGE"
        with self.assertRaises(ClaudeStreamError):
            self.feed(self.reader(), [ack, self.result])

    def test_processed_image_sources_and_bytes_are_closed_and_bounded(self) -> None:
        for change in (
            {"type": "url", "url": "https://example.com/image.png"},
            {"path": "/home/example/image.png"},
            {"media_type": "image/gif", "data": base64.b64encode(b"GIF89a").decode()},
            {"data": "!invalid!"},
            {
                "data": base64.b64encode(
                    b"\x89PNG\r\n\x1a\n" + b"x" * MAX_CLAUDE_IMAGE_BYTES
                ).decode()
            },
        ):
            ack = deepcopy(self.ack)
            ack["message"]["content"][2]["source"].update(change)
            with self.subTest(fields=tuple(change)), self.assertRaises(ClaudeStreamError):
                self.feed(self.reader(), [ack, self.result])

    def test_only_exact_completed_lifecycle_may_follow_terminal(self) -> None:
        for event in (
            self.ack,
            self.lifecycle("started"),
            {**self.lifecycle("completed"), "command_uuid": str(uuid.uuid4())},
            {"type": "assistant", "session_id": SESSION},
        ):
            with self.subTest(kind=event["type"]), self.assertRaises(ClaudeStreamError):
                self.feed(self.reader(), [self.ack, self.result, event])

    def test_typed_failure_without_receipt_is_preserved_but_policy_drift_refuses(self) -> None:
        failure = {**self.result, "subtype": "error_during_execution", "is_error": True}
        self.assertIn("error_during_execution", self.feed(self.reader(), [failure]))
        for event in (
            {"type": "control_request"},
            {"type": "system", "subtype": "init", "tools": ["ExampleTool"]},
        ):
            with (
                self.subTest(kind=event["type"]),
                self.assertRaisesRegex(ClaudeStreamError, "policy"),
            ):
                self.feed(self.reader(), [event, self.ack, self.result])

    def test_unterminated_input_ack_obeys_its_own_frame_bound(self) -> None:
        reader = self.reader()
        with self.assertRaisesRegex(ClaudeStreamError, "event exceeded"):
            reader.feed(b"x" * (8 * 1024 * 1024 + 1))

    def test_maximum_aggregate_receipt_is_streamed_and_discarded(self) -> None:
        large = image().data
        large += b"x" * (MAX_CLAUDE_IMAGE_BYTES - len(large))
        data = encode_claude_image_input(
            "Example maximum bundle",
            (image(large), image(large, position=2)),
            SESSION,
        )
        ack = {**json.loads(data), "isReplay": True}
        reader = ClaudeStreamReader(
            expected_session_id=SESSION,
            image_receipt=ClaudeImageReceipt(data, expected_session_id=SESSION),
        )
        raw = (json.dumps(ack) + "\n" + json.dumps(self.result) + "\n").encode()
        for start in range(0, len(raw), 65536):
            reader.feed(raw[start : start + 65536])
        self.assertEqual(len(reader.finish().splitlines()), 1)
        self.assertLess(len(reader._output), 1024)

    def test_ordinary_retained_output_keeps_the_text_limit(self) -> None:
        reader = self.reader()
        ack = json.dumps(self.ack).encode() + b"\n"
        reader.feed(ack)
        large = {"type": "system", "subtype": "example", "text": "x" * (2 * 1024 * 1024)}
        with self.assertRaisesRegex(ClaudeStreamError, "retained visible output exceeded"):
            reader.feed(json.dumps(large).encode() + b"\n")


if __name__ == "__main__":
    unittest.main()
