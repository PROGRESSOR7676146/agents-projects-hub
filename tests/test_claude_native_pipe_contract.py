"""Host expectation and one-use fake response, before any native pipe wiring."""

from __future__ import annotations

import hashlib
import json
import struct
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

from hermes_codex_router.review_bridge_protocol import BridgeFrame, BridgeFrameType
from tests.claude_native_pipe_contract import (
    MAX_HEADER_BYTES,
    NativePipeAttempt,
    NativePipeContractError,
    decode_native_request,
    encode_native_request,
    native_pipe_prompt,
)
from tests.claude_native_request_contract import (
    DUMMY,
    MAX_REQUEST_BYTES,
    NATIVE_SESSION_ID,
    POST_HEADERS,
    ExpectedNativeRequest,
)
from tests.test_claude_native_request_contract import EXAMPLE_ENVIRONMENT, valid_body

PORT = 12345


def capsule_bytes(text: str = "Example host-selected material.\n") -> bytes:
    raw = text.encode()
    return json.dumps(
        {
            "version": 1,
            "binding": "example-pipe-review",
            "files": [
                {
                    "name": "example.txt",
                    "text": text,
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def header_bytes(
    raw: bytes, extras: bytes = b"", *, port: int = PORT, case: str = "bearer-success"
) -> bytes:
    pairs = [
        *POST_HEADERS.items(),
        ("Host", "127.0.0.1:" + str(port)),
        ("Content-Length", str(len(raw))),
        ("X-Claude-Code-Session-Id", NATIVE_SESSION_ID),
        ("Authorization", "Bearer " + DUMMY) if case == "bearer-success" else ("x-api-key", DUMMY),
    ]
    return (
        b"POST /v1/messages?beta=true HTTP/1.1\r\n"
        + b"".join((key + ": " + value + "\r\n").encode() for key, value in pairs)
        + extras
        + b"\r\n"
    )


def selected_body(capsule: bytes) -> bytes:
    body = valid_body()
    body["messages"][0]["content"] = native_pipe_prompt(capsule)
    return json.dumps(body).encode()


def request_frame(raw: bytes, headers: bytes | None = None) -> BridgeFrame:
    return BridgeFrame(
        BridgeFrameType.REQUEST,
        encode_native_request(header_bytes(raw) if headers is None else headers, raw),
    )


def attempt(capsule: bytes | None = None, *, response: bytes = b"example-host-response"):
    capsule = capsule_bytes() if capsule is None else capsule
    return NativePipeAttempt(
        capsule,
        ExpectedNativeRequest("2.1.285", EXAMPLE_ENVIRONMENT, native_pipe_prompt(capsule)),
        port=PORT,
        case="bearer-success",
        response=response,
        timeout_seconds=75,
    )


class NativeRequestEnvelopeTests(unittest.TestCase):
    def test_exact_header_and_body_bytes_survive_roundtrip(self) -> None:
        raw = selected_body(capsule_bytes("Example non-ASCII материал.\n"))
        headers = header_bytes(raw)
        decoded = decode_native_request(encode_native_request(headers, raw))
        self.assertEqual(decoded.raw_headers, headers)
        self.assertEqual(decoded.body, raw)
        self.assertEqual(decoded.headers[-1], ("Authorization", "Bearer " + DUMMY))
        self.assertNotIn(DUMMY, repr(decoded))
        self.assertNotIn("material", repr(decoded))

    def test_duplicate_nonlength_headers_survive_until_host_validation(self) -> None:
        raw = selected_body(capsule_bytes())
        headers = header_bytes(raw, b"hOsT: 127.0.0.1:12345\r\n")
        decoded = decode_native_request(encode_native_request(headers, raw))
        self.assertEqual([k.lower() for k, _ in decoded.headers].count("host"), 2)
        self.assertEqual(decoded.raw_headers, headers)
        owner = attempt()
        with self.assertRaises(NativePipeContractError):
            owner.submit(request_frame(raw, headers))
        self.assertFalse(owner.observation.attempted)

    def test_duplicate_json_and_embedded_metadata_are_never_reencoded(self) -> None:
        raw = selected_body(capsule_bytes())
        mutations = [raw.replace(b'"model":', b'"model":"example-other","model":', 1)]
        body = json.loads(raw)
        body["metadata"]["user_id"] = body["metadata"]["user_id"].replace(
            '"session_id":', '"session_id":"example-other","session_id":', 1
        )
        mutations.append(json.dumps(body).encode())
        for changed in mutations:
            with self.subTest(raw=changed[:16]):
                frame = request_frame(changed)
                self.assertEqual(decode_native_request(frame.payload).body, changed)
                owner = attempt()
                with self.assertRaises(NativePipeContractError):
                    owner.submit(frame)
                self.assertFalse(owner.observation.attempted)

    def test_exact_byte_limits_and_next_byte(self) -> None:
        raw = b"x" * MAX_REQUEST_BYTES
        self.assertEqual(
            len(encode_native_request(b"x" * MAX_HEADER_BYTES, raw)),
            4 + MAX_HEADER_BYTES + MAX_REQUEST_BYTES,
        )
        prefix = b"POST /v1/messages HTTP/1.1\r\nContent-Length: 16384\r\n"
        # 24 bounded headers, padded only with valid value bytes.
        lines = [b"example-" + str(i).encode() + b": " + b"x" * 512 + b"\r\n" for i in range(23)]
        headers = prefix + b"".join(lines) + b"\r\n"
        self.assertLessEqual(len(headers), MAX_HEADER_BYTES)
        decoded = decode_native_request(encode_native_request(headers, raw))
        self.assertEqual(decoded.body, raw)
        self.assertEqual(len(decoded.headers), 24)
        for headers, body in ((b"x" * (MAX_HEADER_BYTES + 1), b"x"), (b"x", raw + b"x")):
            with self.assertRaises(NativePipeContractError):
                encode_native_request(headers, body)
        for payload in (
            struct.pack("!I", MAX_HEADER_BYTES + 1) + b"x" * (MAX_HEADER_BYTES + 1) + b"x",
            struct.pack("!I", 0) + b"x",
        ):
            with self.assertRaisesRegex(
                NativePipeContractError, "^example_native_pipe_header_bound$"
            ):
                decode_native_request(payload)
        with self.assertRaises(NativePipeContractError):
            decode_native_request(struct.pack("!I", 1) + b"x" + b"x" * (MAX_REQUEST_BYTES + 1))

    def test_truncated_prefix_header_body_and_trailing_bytes_refuse(self) -> None:
        raw = selected_body(capsule_bytes())
        encoded = encode_native_request(header_bytes(raw), raw)
        for invalid in (b"", encoded[:3], encoded[:4], encoded[:40], encoded[:-1], encoded + b"x"):
            with self.subTest(size=len(invalid)), self.assertRaises(NativePipeContractError):
                decode_native_request(invalid)

    def test_request_line_and_header_grammar_refuse_before_semantics(self) -> None:
        raw = b"x"
        valid = b"POST /v1/messages HTTP/1.1\r\nContent-Length: 1\r\n\r\n"
        for changed in (
            valid.replace(b"POST", b"GET", 1),
            valid.replace(b"/v1/messages", b"http://example.com/v1/messages", 1),
            valid.replace(b"/v1/messages", b"/v1/messages?other=true", 1),
            valid.replace(b"HTTP/1.1", b"HTTP/1.0", 1),
            valid.replace(b"\r\n", b"\n"),
            valid.replace(b"\r\n\r\n", b"\r\n"),
            valid.replace(b"Content-Length: 1", b" Content-Length: 1"),
            valid.replace(b"Content-Length: 1", b"Content-Length: 01"),
            valid.replace(b"Content-Length: 1", b"Content-Length: +1"),
            valid.replace(b"Content-Length: 1", b"Content-Length: 1\r\ncontent-length: 1"),
            valid.replace(b"Content-Length: 1", b"Content-Length: 1\r\nTransfer-Encoding: chunked"),
            valid.replace(b"Content-Length: 1", b"example: x"),
            valid.replace(b"Content-Length: 1", b"Content-Length: 1\r\nexample: x\x00"),
            valid.replace(b"Content-Length: 1", b"Content-Length: 1\r\nexample: \xff"),
            valid.replace(b"\r\n\r\n", b"\r\n" + b"example: x\r\n" * 24 + b"\r\n"),
        ):
            with self.subTest(headers=changed[:32]), self.assertRaises(NativePipeContractError):
                decode_native_request(encode_native_request(changed, raw))


class NativePipeAttemptTests(unittest.TestCase):
    def test_api_key_and_response_upper_bound_positive(self) -> None:
        capsule = capsule_bytes()
        owner = NativePipeAttempt(
            capsule,
            ExpectedNativeRequest("2.1.285", EXAMPLE_ENVIRONMENT, native_pipe_prompt(capsule)),
            port=PORT,
            case="api-key-success",
            response=b"x" * (64 * 1024),
        )
        raw = selected_body(capsule)
        self.assertEqual(
            owner.submit(request_frame(raw, header_bytes(raw, case="api-key-success"))),
            b"x" * (64 * 1024),
        )
        self.assertTrue(owner.observation.attempted)

    def test_wrong_frame_type_never_runs_its_attribute_code(self) -> None:
        class HostileObject:
            @property
            def kind(self) -> object:
                raise AssertionError("example-attribute-code-must-not-run")

        owner = attempt()
        with self.assertRaises(NativePipeContractError):
            owner.submit(HostileObject())
        self.assertFalse(owner.observation.attempted)

    def test_prepared_response_and_host_capsule_positive(self) -> None:
        capsule = capsule_bytes()
        owner = attempt(capsule)
        self.assertEqual(owner.capsule_bytes, capsule)
        self.assertEqual(
            owner.submit(request_frame(selected_body(capsule))), b"example-host-response"
        )
        self.assertTrue(owner.observation.attempted)
        self.assertTrue(owner.observation.retired)
        with self.assertRaises(NativePipeContractError):
            owner.submit(request_frame(selected_body(capsule)))

    def test_substituted_material_with_same_controls_gets_zero_response(self) -> None:
        owner = attempt()
        substituted = selected_body(capsule_bytes("Example substituted material.\n"))
        with self.assertRaises(NativePipeContractError):
            owner.submit(request_frame(substituted))
        self.assertFalse(owner.observation.attempted)
        self.assertTrue(owner.observation.retired)
        with self.assertRaises(NativePipeContractError):
            owner.submit(request_frame(selected_body(capsule_bytes())))

    def test_constructor_rejects_capsule_expectation_mismatch_and_bad_capsule(self) -> None:
        for capsule in (capsule_bytes("Example different selection."), b"{}", b"", b"\xff"):
            with self.subTest(size=len(capsule)), self.assertRaises(NativePipeContractError):
                NativePipeAttempt(
                    capsule,
                    ExpectedNativeRequest(
                        "2.1.285", EXAMPLE_ENVIRONMENT, native_pipe_prompt(capsule_bytes())
                    ),
                    port=PORT,
                    case="bearer-success",
                    response=b"example",
                )

    def test_invalid_first_frame_cannot_be_repaired_by_valid_second(self) -> None:
        for frame in (
            BridgeFrame(BridgeFrameType.CAPSULE, capsule_bytes()),
            BridgeFrame(BridgeFrameType.REQUEST, b"example-malformed"),
            object(),
            object.__new__(BridgeFrame),
        ):
            owner = attempt()
            with self.subTest(frame=type(frame)), self.assertRaises(NativePipeContractError):
                owner.submit(frame)
            self.assertFalse(owner.observation.attempted)
            with self.assertRaises(NativePipeContractError):
                owner.submit(request_frame(selected_body(capsule_bytes())))

    def test_host_port_is_not_learned_from_child_headers(self) -> None:
        raw = selected_body(capsule_bytes())
        owner = attempt()
        with self.assertRaises(NativePipeContractError):
            owner.submit(request_frame(raw, header_bytes(raw, port=PORT + 1)))
        self.assertFalse(owner.observation.attempted)

    def test_concurrent_valid_submissions_return_only_one_response(self) -> None:
        owner = attempt()
        frame = request_frame(selected_body(capsule_bytes()))
        barrier = threading.Barrier(2)

        def submit() -> bool:
            barrier.wait(timeout=2)
            try:
                return owner.submit(frame) == b"example-host-response"
            except NativePipeContractError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: submit(), range(2))), [False, True])
        self.assertTrue(owner.observation.attempted)

    def test_cancellation_before_and_after_consumption_preserves_distinction(self) -> None:
        for after in (False, True):
            owner = attempt()
            frame = request_frame(selected_body(capsule_bytes()))
            if after:
                owner.submit(frame)
            owner.cancel()
            owner.close()
            with self.assertRaises(NativePipeContractError):
                owner.submit(frame)
            self.assertEqual(owner.observation.attempted, after)
            self.assertTrue(owner.observation.revoked)

    def test_fixed_deadline_invalid_and_backwards_clocks_retire(self) -> None:
        for finish in (175, 176, 99, float("nan"), float("inf"), True):
            with patch("tests.claude_native_pipe_contract._monotonic", side_effect=[100, finish]):
                owner = attempt()
                with self.subTest(finish=finish), self.assertRaises(NativePipeContractError):
                    owner.submit(request_frame(selected_body(capsule_bytes())))
                self.assertFalse(owner.observation.attempted)
                self.assertTrue(owner.observation.retired)

    def test_clock_seam_accepts_before_deadline_and_rejects_invalid_construction(self) -> None:
        with patch("tests.claude_native_pipe_contract._monotonic", side_effect=[100, 174.99]):
            self.assertEqual(
                attempt().submit(request_frame(selected_body(capsule_bytes()))),
                b"example-host-response",
            )
        for invalid in (float("nan"), float("inf"), -1, True):
            with patch("tests.claude_native_pipe_contract._monotonic", return_value=invalid):
                with self.assertRaises(NativePipeContractError):
                    attempt()

    def test_expected_values_are_snapshotted_before_child_submission(self) -> None:
        capsule = capsule_bytes()
        expected = ExpectedNativeRequest(
            "2.1.285", EXAMPLE_ENVIRONMENT, native_pipe_prompt(capsule)
        )
        owner = NativePipeAttempt(
            capsule, expected, port=PORT, case="bearer-success", response=b"example"
        )
        object.__setattr__(expected, "prompt", "example-substitution")
        self.assertEqual(owner.submit(request_frame(selected_body(capsule))), b"example")
        self.assertNotIn("Example host-selected", repr(owner))

    def test_host_types_bounds_and_error_chains_are_fixed(self) -> None:
        for changes in (
            {"response": bytearray(b"example")},
            {"response": b"x" * (64 * 1024 + 1)},
            {"response": b""},
            {"port": True},
            {"port": 0},
            {"port": 65536},
            {"case": "example-unknown"},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": True},
            {"timeout_seconds": 0},
            {"timeout_seconds": -1},
            {"timeout_seconds": 106},
        ):
            values: dict[str, Any] = dict(port=PORT, case="bearer-success", response=b"example")
            values.update(changes)
            with self.assertRaises(NativePipeContractError):
                NativePipeAttempt(
                    capsule_bytes(),
                    ExpectedNativeRequest(
                        "2.1.285", EXAMPLE_ENVIRONMENT, native_pipe_prompt(capsule_bytes())
                    ),
                    **values,
                )
        raw = selected_body(capsule_bytes()).replace(
            b'"model":', b'"model":"example-private","model":', 1
        )
        with self.assertRaises(NativePipeContractError) as raised:
            attempt().submit(request_frame(raw))
        self.assertEqual(str(raised.exception), "example_native_pipe_request_invalid")
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)


if __name__ == "__main__":
    unittest.main()
