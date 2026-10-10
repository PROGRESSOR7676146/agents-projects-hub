"""Images and restored history must match before any fake response is served."""

from __future__ import annotations

import copy
import json
import os
import threading
import unittest
from contextlib import contextmanager
from typing import Iterator

from tests.claude_image_request_contract import (
    MISSING_SESSION_ID,
    expected_messages,
    input_message,
    validate_image_request,
    validate_missing_session_result,
)
from tests.claude_image_session_actor import ImageServer
from tests.claude_native_request_contract import (
    HEAD_HEADERS,
    ExpectedNativeRequest,
    NativeRequestContractError,
)
from tests.test_claude_native_request_contract import EXAMPLE_ENVIRONMENT, request, valid_body


@contextmanager
def running_image_server(phase: int = 0) -> Iterator[ImageServer]:
    server = ImageServer(0, phase=phase)
    server.request_contract = ExpectedNativeRequest("2.1.285", EXAMPLE_ENVIRONMENT)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if thread.is_alive():
            raise AssertionError("fictional image listener cleanup failed")


class ClaudeImageRequestTests(unittest.TestCase):
    expected = ExpectedNativeRequest("2.1.285", EXAMPLE_ENVIRONMENT)

    def body(self, phase: int = 0) -> dict:
        body = valid_body()
        body["messages"] = expected_messages(self.expected, phase, uid=1234)
        return body

    def validate(self, body: dict, phase: int = 0) -> None:
        validate_image_request(json.dumps(body).encode(), self.expected, phase, uid=1234)

    def test_fresh_and_resumed_requests_validate(self) -> None:
        self.validate(self.body())
        self.validate(self.body(1), 1)
        for phase in (0, 1):
            envelope = json.loads(input_message(phase))
            self.assertEqual(
                envelope["message"]["content"][:1],
                self.body(phase)["messages"][-2]["content"][:1]
                if phase
                else self.body()["messages"][0]["content"][:1],
            )
            self.assertEqual(len(envelope["message"]["content"]), 2)
            self.assertNotIn("example-image-1", input_message(phase).decode())

    def test_changed_image_caption_mime_path_order_or_extra_block_refuses(self) -> None:
        mutations = (
            lambda b: b["messages"][0]["content"][0].update(text="other caption"),
            lambda b: b["messages"][0]["content"][1]["source"].update(data="eA=="),
            lambda b: b["messages"][0]["content"][1]["source"].update(data="%%%"),
            lambda b: b["messages"][0]["content"][1]["source"].update(media_type="image/jpeg"),
            lambda b: b["messages"][0]["content"][1].pop("source"),
            lambda b: b["messages"][0]["content"][1].update(type="text", text="image unavailable"),
            lambda b: b["messages"][0]["content"][2].update(text="[Image: source: /other/root]"),
            lambda b: b["messages"][0]["content"].reverse(),
            lambda b: b["messages"][0]["content"].append({"type": "text", "text": "extra"}),
            lambda b: b.update(tools=[{"name": "Read"}]),
            lambda b: b["metadata"].update(user_id="{}"),
            lambda b: b["system"][2].update(text="unselected system"),
        )
        for mutate in mutations:
            body = self.body()
            mutate(body)
            with (
                self.subTest(mutation=mutations.index(mutate)),
                self.assertRaises(NativeRequestContractError),
            ):
                self.validate(body)

    def test_resume_requires_exact_prior_user_assistant_and_native_scaffolds(self) -> None:
        for index in range(5):
            body = self.body(1)
            body["messages"].pop(index)
            with self.subTest(index=index), self.assertRaises(NativeRequestContractError):
                self.validate(body, 1)
        for index in (0, 2, 3):
            body = self.body(1)
            body["messages"][index]["content"][0]["text"] = "foreign history"
            with self.subTest(index=index), self.assertRaises(NativeRequestContractError):
                self.validate(body, 1)
        body = self.body(1)
        body["messages"][1]["content"] = "foreign scaffold"
        with self.assertRaises(NativeRequestContractError):
            self.validate(body, 1)

    def test_malformed_duplicate_and_non_pinned_contract_refuse(self) -> None:
        raw = json.dumps(self.body()).encode()
        for malformed in (
            raw[:-1],
            raw.replace(b'"tools": []', b'"tools": [], "tools": []'),
            b"x" * 20000,
        ):
            with self.subTest(length=len(malformed)), self.assertRaises(NativeRequestContractError):
                validate_image_request(malformed, self.expected, 0, uid=1234)
        for phase in (True, -1, 2):
            with self.subTest(phase=phase), self.assertRaises(NativeRequestContractError):
                validate_image_request(raw, self.expected, phase, uid=1234)
        with self.assertRaises(NativeRequestContractError):
            validate_image_request(
                raw, ExpectedNativeRequest("0.0.0", EXAMPLE_ENVIRONMENT), 0, uid=1234
            )

    def test_validation_never_mutates_selected_or_received_body(self) -> None:
        body = self.body(1)
        before = copy.deepcopy(body)
        self.validate(body, 1)
        self.assertEqual(body, before)

    def test_missing_session_requires_one_explicit_validated_failure(self) -> None:
        result = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "session_id": MISSING_SESSION_ID,
            "errors": ["No conversation found with session ID: " + MISSING_SESSION_ID],
        }
        raw = json.dumps(result).encode()
        validate_missing_session_result(raw, returncode=1)
        validate_missing_session_result(raw, returncode=0)
        for changes in (
            {"subtype": "success"},
            {"is_error": False},
            {"session_id": "00000000-0000-4000-8000-000000000001"},
            {"errors": ["other failure"]},
            {"result": "example-success"},
            {"api_error_status": 529},
            {"type": "assistant"},
        ):
            with self.subTest(changes=changes), self.assertRaises(NativeRequestContractError):
                validate_missing_session_result(
                    json.dumps({**result, **changes}).encode(), returncode=1
                )
        for bad in (
            b"",
            raw[:-1],
            raw + b"\n" + raw,
            raw.replace(b'"is_error": true', b'"is_error": false,"is_error": true'),
        ):
            with self.subTest(length=len(bad)), self.assertRaises(NativeRequestContractError):
                validate_missing_session_result(bad, returncode=1)
        for code in (-9, True, 256):
            with self.subTest(code=code), self.assertRaises(NativeRequestContractError):
                validate_missing_session_result(raw, returncode=code)


class ImageRequestHandlerTests(unittest.TestCase):
    expected = ClaudeImageRequestTests.expected

    def wire_body(self, phase: int) -> bytes:
        body = valid_body()
        body["messages"] = expected_messages(self.expected, phase, uid=os.getuid())
        return json.dumps(body).encode()

    def test_two_distinct_phases_and_duplicate_post_refusal(self) -> None:
        with running_image_server() as server:
            self.assertEqual(request(server, self.wire_body(0))[0], 200)
            self.assertEqual(request(server, self.wire_body(0))[0], 400)
            self.assertEqual((server.validated_requests, server.messages_served), (1, 1))
        with running_image_server(1) as server:
            self.assertEqual(request(server, self.wire_body(1))[0], 200)
            self.assertEqual(request(server, self.wire_body(1))[0], 400)
            self.assertEqual((server.validated_requests, server.messages_served), (1, 1))
            self.assertEqual(server.violations, 1)

    def test_resume_request_cannot_advance_fixture_phase(self) -> None:
        with running_image_server() as server:
            self.assertEqual(request(server, self.wire_body(0))[0], 200)
            self.assertEqual(request(server, self.wire_body(1))[0], 400)
            self.assertEqual((server.phase, server.validated_requests), (0, 1))

    def test_changed_image_or_missing_resume_history_serves_no_success(self) -> None:
        with running_image_server() as server:
            raw = self.wire_body(0).replace(
                b'"media_type": "image/png"', b'"media_type": "image/jpeg"'
            )
            self.assertEqual(request(server, raw)[0], 400)
            self.assertEqual((server.validated_requests, server.messages_served), (0, 0))
        with running_image_server(1) as server:
            self.assertEqual(request(server, self.wire_body(0))[0], 400)
            self.assertEqual((server.validated_requests, server.messages_served), (0, 0))

    def head(self, server: ImageServer, *, path: str = "/api/hello", extra: bool = False) -> int:
        headers = [*HEAD_HEADERS.items(), ("Host", "127.0.0.1:" + str(server.server_port))]
        if extra:
            headers.append(("x-extra", "example"))
        return request(server, b"", headers, method="HEAD", path=path)[0]

    def test_each_invocation_has_one_strict_passive_head(self) -> None:
        for phase in (0, 1, 2):
            with self.subTest(phase=phase), running_image_server(phase) as server:
                self.assertEqual(self.head(server), 200)
                self.assertGreaterEqual(self.head(server), 400)
                self.assertEqual(server.messages_served, 0)
                self.assertGreater(server.violations, 0)
        for path, extra in (("/other", False), ("/api/hello", True)):
            with self.subTest(path=path, extra=extra), running_image_server(2) as server:
                self.assertEqual(self.head(server, path=path, extra=extra), 400)
                self.assertEqual(server.messages_served, 0)

    def test_negative_invocation_counts_attempted_post_and_never_serves_success(self) -> None:
        with running_image_server(2) as server:
            self.assertEqual(request(server, self.wire_body(0))[0], 400)
            self.assertEqual(server.posts, 1)
            self.assertEqual((server.validated_requests, server.messages_served), (0, 0))
            self.assertGreater(server.violations, 0)
