"""Closed native input envelopes: verified bytes, never paths or arbitrary JSON."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
import uuid
from dataclasses import replace

from hermes_codex_router.claude_image_input import (
    ClaudeImageInputError,
    VerifiedClaudeImage,
    encode_claude_image_input,
)

SESSION = "00000000-0000-4000-8000-000000000001"
PNG = b"\x89PNG\r\n\x1a\nfictional-png-bytes"
JPEG = b"\xff\xd8\xfffictional-jpeg-bytes"


def image(data: bytes = PNG, mime: str = "image/png", position: int = 1) -> VerifiedClaudeImage:
    return VerifiedClaudeImage(position, mime, data, hashlib.sha256(data).hexdigest())


class ClaudeImageInputTests(unittest.TestCase):
    def test_one_new_user_frame_keeps_prompt_and_ordered_exact_bytes(self) -> None:
        prompt = 'New caption \u00b7 \u041f\u0440\u0438\u043c\u0435\u0440\nwith a quote: "example"'
        data = encode_claude_image_input(prompt, (image(), image(JPEG, "image/jpeg", 3)), SESSION)
        self.assertEqual(len(data.splitlines()), 1)
        value = json.loads(data)
        self.assertEqual(
            set(value), {"type", "session_id", "message", "parent_tool_use_id", "uuid"}
        )
        self.assertEqual(str(uuid.UUID(value["uuid"])), value["uuid"])
        self.assertEqual(value["type"], "user")
        self.assertEqual(value["session_id"], SESSION)
        self.assertIsNone(value["parent_tool_use_id"])
        self.assertEqual(value["message"]["role"], "user")
        blocks = value["message"]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": prompt})
        self.assertEqual(
            [block["text"] for block in blocks[1::2]], ["MATERIAL 1 IMAGE", "MATERIAL 3 IMAGE"]
        )
        for block, expected, mime in (
            (blocks[2], PNG, "image/png"),
            (blocks[4], JPEG, "image/jpeg"),
        ):
            self.assertEqual(set(block), {"type", "source"})
            self.assertEqual(set(block["source"]), {"type", "media_type", "data"})
            self.assertEqual(block["source"]["type"], "base64")
            self.assertEqual(block["source"]["media_type"], mime)
            self.assertEqual(base64.b64decode(block["source"]["data"], validate=True), expected)

    def test_bad_source_metadata_or_bytes_cannot_be_serialized(self) -> None:
        original = image()
        for changed in (
            replace(original, data="/home/example/image.png"),  # type: ignore[arg-type]
            replace(original, data=b"not-png"),
            replace(original, media_type="image/jpeg"),
            replace(original, sha256="0" * 64),
            replace(original, position=True),
            replace(original, position=0),
            replace(original, media_type="image/webp"),
            {"type": "image", "source": "/home/example/image.png"},
        ):
            with (
                self.subTest(changed=type(changed).__name__),
                self.assertRaises(ClaudeImageInputError),
            ):
                encode_claude_image_input("Example", (changed,), SESSION)  # type: ignore[arg-type]

    def test_identity_order_and_count_fail_closed(self) -> None:
        for images, session, prompt in (
            ((), SESSION, "Example"),
            ((image(), image()), SESSION, "Example"),
            ((image(position=2), image(position=1)), SESSION, "Example"),
            ((image(),), "not-a-uuid", "Example"),
            ((image(),), SESSION, ""),
            (tuple(image(position=i) for i in range(1, 12)), SESSION, "Example"),
        ):
            with self.subTest(session=session), self.assertRaises(ClaudeImageInputError):
                encode_claude_image_input(prompt, images, session)

    def test_image_and_aggregate_caps_are_in_bytes(self) -> None:
        too_large = PNG + b"x" * (2 * 1024 * 1024)
        aggregate = PNG + b"x" * (2 * 1024 * 1024 - len(PNG))
        for images in ((image(too_large),), tuple(image(aggregate, position=i) for i in (1, 2, 3))):
            with self.assertRaises(ClaudeImageInputError):
                encode_claude_image_input("Example", images, SESSION)

    def test_final_utf8_ndjson_cap_includes_escaping_and_base64(self) -> None:
        for prompt in ("\u043f" * (8 * 1024 * 1024), "\x00" * (2 * 1024 * 1024)):
            with self.assertRaises(ClaudeImageInputError):
                encode_claude_image_input(prompt, (image(),), SESSION)

    def test_payload_bytes_are_absent_from_dto_representation(self) -> None:
        value = image(PNG + b"private-example-material")
        self.assertNotIn("private-example-material", repr(value))


if __name__ == "__main__":
    unittest.main()
