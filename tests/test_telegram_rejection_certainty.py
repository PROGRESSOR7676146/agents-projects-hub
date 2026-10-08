"""Exercise the urllib boundary, rather than constructing a classified rejection."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

from hermes_codex_router.delivery_retry import proven_delivery_rejection
from hermes_codex_router.telegram import TelegramBotApi, TelegramError


class TelegramRejectionCertaintyTests(unittest.TestCase):
    def assert_response(
        self, status: int, body: object, *, expected_class: str, retryable: bool
    ) -> None:
        for document in (False, True):
            with self.subTest(document=document, status=status, body=body):
                encoded = body if isinstance(body, bytes) else json.dumps(body).encode()

                def opener(*_args: Any, **_kwargs: Any) -> io.BytesIO:
                    if status != 200:
                        raise urllib.error.HTTPError(
                            "https://example.com/private-request",
                            status,
                            "private detail",
                            Message(),
                            io.BytesIO(encoded),
                        )
                    return io.BytesIO(encoded)

                bot = TelegramBotApi("123456:example", opener=opener)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "example.md"
                    path.write_bytes(b"Example attachment")
                    with self.assertRaises(TelegramError) as raised:
                        if document:
                            bot.send_document(-1001234567890, 77, path)
                        else:
                            bot.send_html(-1001234567890, 77, "Example result")
                error = raised.exception
                self.assertEqual(error.failure_class, expected_class)
                self.assertEqual(proven_delivery_rejection(error), retryable)
                self.assertNotIn("private", str(error))
                self.assertNotIn(
                    "private", error.safe_detail(consecutive_failures=1, last_success=None)
                )

    def test_real_http_telegram_rejections_are_proven_for_text_and_documents(self) -> None:
        for status in (400, 403, 429):
            self.assert_response(
                status,
                {"ok": False, "error_code": status, "description": "private detail"},
                expected_class="api_rejection",
                retryable=True,
            )

    def test_http_ambiguity_or_mismatched_body_never_proves_rejection(self) -> None:
        for status, body in (
            (400, b"<html>private proxy error</html>"),
            (400, {"error_code": 400}),
            (400, {"ok": 0, "error_code": 400}),
            (400, {"ok": False, "error_code": 403}),
            (403, {"ok": False, "error_code": "403"}),
            (400, {"ok": False, "error_code": True}),
            (408, {"ok": False, "error_code": 408}),
            (500, {"ok": False, "error_code": 400}),
            (500, {"ok": False, "error_code": 500}),
        ):
            self.assert_response(status, body, expected_class="api_http", retryable=False)

    def test_http_200_requires_explicit_false_and_integer_error_code(self) -> None:
        for body in (
            {"error_code": 400},
            {"ok": 0, "error_code": 400},
            {"ok": False, "error_code": "400"},
            {"ok": False, "error_code": True},
        ):
            self.assert_response(200, body, expected_class="invalid_response", retryable=False)
        self.assert_response(
            200, {"ok": False, "error_code": 400}, expected_class="api_rejection", retryable=True
        )
