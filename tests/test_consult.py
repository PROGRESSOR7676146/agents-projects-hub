"""One explicit consultation request, visible text only, no fallback or retry."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hermes_codex_router import consult


class ConsultTests(unittest.TestCase):
    def test_usage_error_is_a_local_refusal_without_request(self) -> None:
        with patch.object(consult, "_request") as request, patch("sys.stderr", io.StringIO()):
            self.assertEqual(consult.main([]), 1)
        request.assert_not_called()

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="example-consult-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root / "client-key"
        self.key.write_text("fictional-inference-only-key")
        self.key.chmod(0o600)
        self.raw = {
            "endpoint": "http://127.0.0.1:19001/v1/responses",
            "credential_file": str(self.key),
            "subscription_route_verified": True,
            "routes": {
                "astra": {
                    "model": "example/astra",
                    "response_model": "example-astra",
                    "effort": "high",
                }
            },
        }
        self.path = self.root / "consult.json"
        self.save()
        self.good = {
            "status": "completed",
            "model": "example-astra",
            "output": [
                {"type": "reasoning", "summary": [{"text": "DO NOT FORWARD"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Visible advice"}],
                },
            ],
        }

    def save(self) -> None:
        self.path.write_text(json.dumps(self.raw))
        self.path.chmod(0o600)

    def invoke(self, request) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with (
            patch.object(consult, "_request", request),
            patch("sys.stdin", io.StringIO("bounded public question")),
            patch("sys.stdout", output),
            patch("sys.stderr", error),
        ):
            status = consult.main(["--config", str(self.path), "astra"])
        return status, output.getvalue(), error.getvalue()

    def test_one_exact_request_and_only_visible_text(self) -> None:
        opener = AsyncMock(return_value=json.dumps(self.good).encode())
        status, visible, error = self.invoke(opener)
        self.assertEqual((status, visible, error), (0, "Visible advice\n", ""))
        self.assertEqual(opener.await_count, 1)
        body = opener.call_args.args[1]
        self.assertEqual(body["model"], "example/astra")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["tool_choice"], "none")
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertNotIn("previous_response_id", body)

    def test_incomplete_wrong_model_tool_output_and_transport_loss_never_retry(self) -> None:
        for payload in (
            self.good | {"status": "incomplete"},
            self.good | {"model": "other"},
            self.good | {"output": [{"type": "function_call", "name": "exec"}]},
            self.good | {"output": []},
        ):
            opener = AsyncMock(return_value=json.dumps(payload).encode())
            status, visible, error = self.invoke(opener)
            self.assertNotEqual(status, 0)
            self.assertEqual(visible, "")
            self.assertNotIn("DO NOT FORWARD", error)
            self.assertEqual(opener.await_count, 1)
        opener = AsyncMock(side_effect=TimeoutError("raw-secret"))
        status, visible, error = self.invoke(opener)
        self.assertEqual(status, 2)
        self.assertIn("unknown", error)
        self.assertNotIn("raw-secret", error)
        self.assertEqual(opener.await_count, 1)

    def test_invalid_config_or_credential_prevents_network(self) -> None:
        for change in (
            {"endpoint": None},
            {"endpoint": ["invalid"]},
            {"endpoint": "https://example.com/v1/responses"},
            {"endpoint": "http://localhost:19001/v1/responses"},
            {"endpoint": "http://127.0.0.1:19001/management"},
            {"subscription_route_verified": False},
            {"fallback": "paid"},
            {"max_output_tokens": True},
            {"timeout_seconds": 0},
        ):
            original = self.raw
            self.raw = original | change
            self.save()
            opener = AsyncMock()
            status, _, _ = self.invoke(opener)
            self.assertEqual(status, 1)
            opener.assert_not_called()
            self.raw = original
        self.save()
        self.key.chmod(0o644)
        opener = AsyncMock()
        self.assertEqual(self.invoke(opener)[0], 1)
        opener.assert_not_called()

    def test_consumed_request_parse_and_output_failures_return_unknown_without_traceback(
        self,
    ) -> None:
        opener = AsyncMock(return_value=b"[" * 2000 + b"]" * 2000)
        status, visible, error = self.invoke(opener)
        self.assertEqual((status, visible), (2, ""))
        self.assertNotIn("Traceback", error)
        self.assertEqual(opener.await_count, 1)
        for failure in (
            BrokenPipeError("raw-secret"),
            UnicodeEncodeError("ascii", "я", 0, 1, "raw-secret"),
        ):
            opener = AsyncMock(return_value=json.dumps(self.good).encode())
            error = io.StringIO()
            with (
                patch.object(consult, "_request", opener),
                patch("sys.stdin", io.StringIO("question")),
                patch("sys.stdout.write", side_effect=failure),
                patch("sys.stderr", error),
            ):
                status = consult.main(["--config", str(self.path), "astra"])
            self.assertEqual(status, 2)
            self.assertEqual(opener.await_count, 1)
            self.assertNotIn("raw-secret", error.getvalue())
