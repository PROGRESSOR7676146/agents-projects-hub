"""Real fixture HTTP handling must refuse changes before a synthetic response."""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from tests.claude_native_request_contract import (
    CAPSULE_BYTES,
    DUMMY,
    HEAD_HEADERS,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_REQUEST_BYTES,
    MODEL,
    NATIVE_SESSION_ID,
    POST_HEADERS,
    PROMPT,
    SYSTEM_PROMPT,
    VENDOR_SYSTEM,
    ExpectedNativeRequest,
    NativeRequestContractError,
    _strict_json,
    environment_text,
    validate_request_body,
)
from tests.claude_native_transport_actor import FixtureServer

EXAMPLE_ENVIRONMENT = environment_text("Linux 0.0.0-example", "2000-01-01")


@contextmanager
def running_fixture(case: str = "bearer-success") -> Iterator[FixtureServer]:
    server = FixtureServer(0, case)
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
            raise AssertionError("example-fixture-cleanup-failed")


def base_headers(server: FixtureServer, raw: bytes) -> list[tuple[str, str]]:
    return [
        *POST_HEADERS.items(),
        ("Host", "127.0.0.1:" + str(server.server_port)),
        ("Content-Length", str(len(raw))),
        ("X-Claude-Code-Session-Id", NATIVE_SESSION_ID),
        ("Authorization", "Bearer " + DUMMY)
        if server.case.startswith("bearer")
        else ("x-api-key", DUMMY),
    ]


def request(
    server: FixtureServer,
    raw: bytes,
    headers: list[tuple[str, str]] | None = None,
    *,
    method: str = "POST",
    path: str = "/v1/messages?beta=true",
) -> tuple[int, bytes]:
    if headers is None:
        headers = base_headers(server, raw)
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as connection:
        wire = (
            method
            + " "
            + path
            + " HTTP/1.1\r\n"
            + "".join(name + ": " + value + "\r\n" for name, value in headers)
        )
        connection.sendall(wire.encode("ascii") + b"\r\n" + raw)
        response = http.client.HTTPResponse(connection)
        response.begin()
        data = response.read()
        if method == "POST" and response.status in {200, 529}:
            if not server.response_complete.wait(timeout=2):
                raise AssertionError("example-fixture-response-incomplete")
        return response.status, data


def valid_body() -> dict:
    cached = {"type": "ephemeral"}
    return {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": PROMPT},
            {
                "role": "system",
                "content": [{"type": "text", "text": EXAMPLE_ENVIRONMENT, "cache_control": cached}],
                "output_config": {"effort": "high"},
            },
        ],
        "system": [
            {
                "type": "text",
                "text": "x-anthropic-billing-header: cc_version=2.1.285.abc; cc_entrypoint=sdk-cli;",
            },
            {"type": "text", "text": VENDOR_SYSTEM, "cache_control": cached},
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": cached},
        ],
        "tools": [],
        "metadata": {
            "user_id": json.dumps(
                {"device_id": "a" * 64, "account_uuid": "", "session_id": NATIVE_SESSION_ID},
                separators=(",", ":"),
            )
        },
        "max_tokens": 1024,
        "thinking": {"type": "adaptive"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": "high"},
        "stream": True,
    }


class NativeRequestHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FixtureServer(0, "bearer-success")
        self.server.request_contract = ExpectedNativeRequest("2.1.285", EXAMPLE_ENVIRONMENT)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01))
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def post(self, body: dict) -> tuple[int, bytes]:
        raw = json.dumps(body).encode("utf-8")
        return request(self.server, raw)

    def test_selected_material_substitution_refuses_before_sse(self) -> None:
        body = copy.deepcopy(valid_body())
        body["messages"][0]["content"] = PROMPT.replace(
            "Example selected material.", "Other material."
        )
        status, response = self.post(body)
        self.assertEqual(status, 400)
        self.assertEqual(response, b"")
        self.assertEqual(self.server.messages_served, 0)

    def test_extra_system_text_refuses_before_sse(self) -> None:
        body = copy.deepcopy(valid_body())
        body["system"].append({"type": "text", "text": "example-extra-material"})
        status, response = self.post(body)
        self.assertEqual(status, 400)
        self.assertEqual(response, b"")
        self.assertEqual(self.server.messages_served, 0)

    def test_synthetic_valid_request_reaches_sse_and_contract_counter(self) -> None:
        status, response = self.post(valid_body())
        self.assertEqual(status, 200)
        self.assertIn(b"example-native-ok", response)
        self.assertEqual(self.server.validated_requests, 1)
        self.assertEqual(self.server.messages_served, 1)

    def test_invalid_first_post_consumes_attempt_across_connections(self) -> None:
        body = valid_body()
        body["output_config"] = None
        self.assertEqual(self.post(body), (400, b""))
        self.assertEqual(self.post(valid_body()), (400, b""))
        self.assertEqual(self.server.posts, 2)
        self.assertEqual(self.server.validated_requests, 0)
        self.assertEqual(self.server.messages_served, 0)

    def test_concurrent_valid_posts_serve_only_one_response_globally(self) -> None:
        ready = threading.Barrier(2)

        def submit() -> tuple[int, bytes]:
            ready.wait(timeout=2)
            return self.post(valid_body())

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(code for code, _ in responses), [200, 400])
        self.assertEqual(self.server.validated_requests, 1)
        self.assertEqual(self.server.messages_served, 1)
        self.assertEqual(self.server.posts, 2)

    def test_optional_head_has_exact_independent_header_contract(self) -> None:
        headers = [*HEAD_HEADERS.items(), ("Host", "127.0.0.1:" + str(self.server.server_port))]
        self.assertEqual(
            request(self.server, b"", headers, method="HEAD", path="/api/hello"), (200, b"")
        )
        self.assertEqual(self.post(valid_body())[0], 200)
        self.assertEqual(
            request(self.server, b"", headers, method="HEAD", path="/api/hello"), (400, b"")
        )
        self.assertEqual(self.server.heads, 2)


class NativeRequestMutationTests(unittest.TestCase):
    def assert_refused(self, raw: bytes, *, case: str = "bearer-success") -> None:
        # Every mutation gets its own server/attempt, so the one-POST guard
        # cannot make an otherwise accepted malformed body look refused.
        with running_fixture(case) as server:
            self.assertEqual(request(server, raw), (400, b""))
            self.assertEqual(server.posts, 1)
            self.assertEqual(server.validated_requests, 0)
            self.assertEqual(server.messages_served, 0)
            self.assertEqual(server.violations, 1)

    def test_top_level_schema_and_control_types(self) -> None:
        for key, value in (
            ("model", "example-other-model"),
            ("stream", 1),
            ("max_tokens", 1024.0),
            ("tools", [{"name": "example-tool"}]),
            ("output_config", None),
            ("output_config", []),
            ("output_config", {"effort": "low"}),
            ("thinking", {"type": "adaptive", "extra": True}),
            ("context_management", {"edits": []}),
            ("metadata", []),
            ("system", {}),
            ("messages", None),
            ("extra", "example-private-value"),
        ):
            with self.subTest(key=key, value=value):
                body = valid_body()
                body[key] = value
                self.assert_refused(json.dumps(body).encode())
        body = valid_body()
        del body["thinking"]
        self.assert_refused(json.dumps(body).encode())

    def test_materials_vendor_environment_roles_and_blocks_are_exact(self) -> None:
        changes = (
            (("messages", 0, "content"), PROMPT + "\nexample-extra-text"),
            (("messages", 0, "content"), [{"type": "text", "text": PROMPT}]),
            (("messages", 0, "role"), "assistant"),
            (("messages", 1, "role"), "user"),
            (("messages", 1, "content", 0, "text"), EXAMPLE_ENVIRONMENT + "extra"),
            (("messages", 1, "content", 0, "cache_control"), {"type": "ephemeral", "ttl": "1h"}),
            (("messages", 1, "output_config"), {"effort": "low"}),
            (
                ("system", 0, "text"),
                "x-anthropic-billing-header: cc_version=2.1.286.abc; cc_entrypoint=sdk-cli;",
            ),
            (
                ("system", 0, "text"),
                "x-anthropic-billing-header: cc_version=2.1.285.abcd; cc_entrypoint=sdk-cli;",
            ),
            (
                ("system", 0, "text"),
                "x-anthropic-billing-header: cc_version=2.1.285.abc; cc_entrypoint=example;",
            ),
            (("system", 1, "text"), VENDOR_SYSTEM + "extra"),
            (("system", 2, "text"), SYSTEM_PROMPT + "extra"),
            (("system", 2, "cache_control"), {}),
        )
        for path, value in changes:
            with self.subTest(path=path, value=value):
                body = valid_body()
                target = body
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                self.assert_refused(json.dumps(body).encode())
        for field in ("messages", "system"):
            body = valid_body()
            body[field].reverse()
            self.assert_refused(json.dumps(body).encode())
            body = valid_body()
            body[field].append(copy.deepcopy(body[field][0]))
            self.assert_refused(json.dumps(body).encode())
        body = valid_body()
        body["messages"][1]["content"].append({"type": "text", "text": "example-extra"})
        self.assert_refused(json.dumps(body).encode())

    def test_metadata_is_bounded_compatibility_and_cannot_substitute_identity(self) -> None:
        for field, value in (
            ("session_id", "019abcde-1234-7fff-8fff-0123456789ab"),
            ("account_uuid", "example-account"),
            ("device_id", "A" * 64),
            ("device_id", "a" * 65),
            ("device_id", []),
            ("extra", "example-private"),
        ):
            body = valid_body()
            metadata = json.loads(body["metadata"]["user_id"])
            metadata[field] = value
            body["metadata"]["user_id"] = json.dumps(metadata)
            self.assert_refused(json.dumps(body).encode())
        for value in (
            "{}",
            "x" * 257,
            [],
            '{"device_id":"a","device_id":"a"}',
            '{"device_id":NaN}',
            "\ud800",
        ):
            body = valid_body()
            body["metadata"]["user_id"] = value
            self.assert_refused(json.dumps(body).encode())
        body = valid_body()
        # The last duplicate value is valid: permissive parsing would allow
        # this full request, so schema mismatch cannot satisfy the assertion.
        metadata = body["metadata"]["user_id"]
        body["metadata"]["user_id"] = metadata.replace(
            '"session_id":',
            '"session_id":"example-other-session","session_id":',
            1,
        )
        self.assert_refused(json.dumps(body).encode())

    def test_json_encoding_duplicates_nonfinite_and_bounds_refuse(self) -> None:
        raw = json.dumps(valid_body()).encode()
        for changed in (
            raw.replace(b'"model":', b'"model":"example-other", "model":', 1),
            raw.replace(b'"effort": "high"', b'"effort": "low", "effort": "high"', 1),
            raw.replace(b'"max_tokens": 1024', b'"max_tokens": NaN'),
            raw.replace(b'"max_tokens": 1024', b'"max_tokens": Infinity'),
            raw.replace(b'"max_tokens": 1024', b'"max_tokens": -Infinity'),
            raw.replace(b'"max_tokens": 1024', b'"max_tokens": 1e9999'),
            raw + b"{}",
            b"\xff",
            b"\xef\xbb\xbf" + raw,
            b'"\\ud800"',
            b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1),
            b"[" + b"0," * MAX_JSON_NODES + b"0]",
        ):
            with self.subTest(size=len(changed)):
                self.assert_refused(changed)

    def test_headers_are_exact_and_duplicates_preserved_before_validation(self) -> None:
        for field, value in (
            ("Host", "example.com"),
            ("Content-Length", "01"),
            ("Content-Length", "99999999999999999999999"),
            ("Content-Length", str(MAX_REQUEST_BYTES + 1)),
            ("Content-Type", "text/plain"),
            ("x-stainless-retry-count", "1"),
            ("X-Claude-Code-Session-Id", "019abcde-1234-7fff-8fff-0123456789ab"),
            ("anthropic-beta", POST_HEADERS["anthropic-beta"] + ",example-extra"),
            ("Authorization", "Bearer example-other"),
            ("Transfer-Encoding", "chunked"),
            ("x-api-key", DUMMY),
            ("example-extra", "example-private-value"),
        ):
            with self.subTest(field=field), running_fixture() as server:
                # Size refusal happens before reading any body. Sending only
                # headers avoids a TCP reset from an unread oversized payload.
                raw = (
                    b"" if field.lower() == "content-length" else json.dumps(valid_body()).encode()
                )
                headers = base_headers(server, raw)
                headers = [(key, entry) for key, entry in headers if key.lower() != field.lower()]
                headers.append((field, value))
                self.assertEqual(request(server, raw, headers), (400, b""))
                self.assertEqual(server.posts, 1)
                self.assertEqual(server.violations, 1)
                self.assertEqual(server.messages_served, 0)
        for duplicate in (
            "HOST",
            "CONTENT-LENGTH",
            "Authorization",
            "anthropic-beta",
            "Content-Type",
        ):
            with self.subTest(duplicate=duplicate), running_fixture() as server:
                raw = json.dumps(valid_body()).encode()
                headers = base_headers(server, raw)
                value = next(value for key, value in headers if key.lower() == duplicate.lower())
                headers.append((duplicate, value))
                self.assertEqual(request(server, raw, headers), (400, b""))
                self.assertEqual(server.posts, 1)
                self.assertEqual(server.violations, 1)
                self.assertEqual(server.messages_served, 0)

    def test_all_credential_outcomes_pass_the_same_handler_contract(self) -> None:
        for case in ("bearer-success", "bearer-reject", "api-key-success", "api-key-reject"):
            with self.subTest(case=case), running_fixture(case) as server:
                status, response = request(server, json.dumps(valid_body()).encode())
                self.assertEqual(status, 529 if case.endswith("reject") else 200)
                self.assertTrue(response)
                self.assertEqual(server.messages_served, 1)
                self.assertEqual(server.validated_requests, 1)
            with running_fixture(case) as server:
                body = valid_body()
                body["messages"][0]["content"] = "example-material-substitution"
                self.assertEqual(request(server, json.dumps(body).encode()), (400, b""))
                self.assertEqual(server.messages_served, 0)

    def test_head_credentials_duplicates_and_nonzero_body_refuse(self) -> None:
        for extra in (
            ("Authorization", "Bearer " + DUMMY),
            ("Content-Length", "1"),
            ("HOST", "127.0.0.1:1"),
            ("example-extra", "example-private"),
        ):
            with self.subTest(extra=extra), running_fixture() as server:
                headers = [
                    *HEAD_HEADERS.items(),
                    ("Host", "127.0.0.1:" + str(server.server_port)),
                    extra,
                ]
                self.assertEqual(
                    request(server, b"", headers, method="HEAD", path="/api/hello"), (400, b"")
                )
                self.assertEqual(server.messages_served, 0)
                self.assertEqual(server.validated_requests, 0)

    def test_unknown_post_path_cannot_receive_a_response(self) -> None:
        with running_fixture() as server:
            self.assertEqual(
                request(
                    server, json.dumps(valid_body()).encode(), path="/v1/messages?example=other"
                ),
                (400, b""),
            )
            self.assertEqual(server.validated_requests, 0)
            self.assertEqual(server.messages_served, 0)


class NativeRequestPureContractTests(unittest.TestCase):
    def test_decoder_strictness_is_independent_of_request_schema(self) -> None:
        # A permissive decoder accepts each grammar below. Call the decoder
        # directly so unrelated Messages schema checks cannot hide a regression.
        for raw in (
            b"NaN",
            b"[Infinity]",
            b"[-Infinity]",
            b"1e9999",
            b'{"a":0,"a":0}',
            b'"\\ud800"',
            b'{"\\udc00":0}',
            b'"\xff"',
            b'"' + b"a" * MAX_REQUEST_BYTES + b'"',
        ):
            with self.subTest(raw=raw[:32]), self.assertRaises(NativeRequestContractError):
                _strict_json(raw)

    def test_fictional_selected_capsule_has_real_verified_material_bytes(self) -> None:
        from hermes_codex_router.review_materials import (
            MaterialSelection,
            build_review_capsule,
            decode_review_capsule,
        )

        document = decode_review_capsule(CAPSULE_BYTES, hashlib.sha256(CAPSULE_BYTES).hexdigest())
        self.assertEqual(document["binding"], "example-review")
        self.assertEqual(PROMPT.split("\n", 1)[1].encode(), CAPSULE_BYTES)
        entry = document["files"][0]
        with tempfile.TemporaryDirectory(prefix="example-native-material-") as directory:
            root = Path(directory)
            (root / entry["name"]).write_text(entry["text"], encoding="utf-8")
            with build_review_capsule(
                root,
                [MaterialSelection(entry["name"], entry["size"], entry["sha256"])],
                binding="example-review",
            ) as capsule:
                self.assertEqual(capsule.read(), CAPSULE_BYTES)

    def test_decoder_accepts_boundary_depth_and_nodes_but_refuses_next_value(self) -> None:
        self.assertIsNotNone(_strict_json(b"[" * MAX_JSON_DEPTH + b"0" + b"]" * MAX_JSON_DEPTH))
        self.assertEqual(
            len(_strict_json(b"[" + b",".join([b"0"] * (MAX_JSON_NODES - 1)) + b"]")),
            MAX_JSON_NODES - 1,
        )
        for raw in (
            b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1),
            b"[" + b",".join([b"0"] * MAX_JSON_NODES) + b"]",
        ):
            with self.assertRaises(NativeRequestContractError):
                _strict_json(raw)

    def test_errors_exclude_received_text_and_exception_chains(self) -> None:
        for raw in (
            b"example-private-json",
            b'{"example-private": NaN}',
            b"\xff",
            b"{}{}",
            b"1e9999",
        ):
            with self.assertRaises(NativeRequestContractError) as raised:
                validate_request_body(raw, ExpectedNativeRequest("2.1.285", EXAMPLE_ENVIRONMENT))
            self.assertEqual(str(raised.exception), "native_request_contract_invalid")
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)
