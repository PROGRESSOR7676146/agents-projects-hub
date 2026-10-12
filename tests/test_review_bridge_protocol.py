from __future__ import annotations

import struct
import unittest
from typing import Any, cast

from hermes_codex_router.review_bridge_protocol import (
    BridgeFrame,
    BridgeFrameDecoder,
    BridgeFrameError,
    BridgeFrameType,
    encode_bridge_frame,
)


class ReviewBridgeProtocolTests(unittest.TestCase):
    def test_fragmented_and_combined_frames_preserve_exact_bytes(self) -> None:
        frames = [
            BridgeFrame(BridgeFrameType.SPEC, b"example-spec"),
            BridgeFrame(BridgeFrameType.CAPSULE, "example-материал".encode()),
            BridgeFrame(BridgeFrameType.RESPONSE_END, b""),
        ]
        wire = b"".join(encode_bridge_frame(frame) for frame in frames)
        for width in (1, 2, 8, 64):
            with self.subTest(width=width):
                decoder = BridgeFrameDecoder()
                observed = []
                for offset in range(0, len(wire), width):
                    observed.extend(decoder.feed(wire[offset : offset + width]))
                decoder.finish()
                self.assertEqual(observed, frames)

    def test_raw_payload_is_absent_from_repr_and_failure(self) -> None:
        frame = BridgeFrame(BridgeFrameType.NATIVE_STDOUT, b"example-private-output")
        self.assertNotIn("example-private-output", repr(frame))
        with self.assertRaises(BridgeFrameError) as raised:
            BridgeFrameDecoder().feed(b"example-private-header")
        self.assertNotIn("example-private", str(raised.exception))

    def test_invalid_header_type_and_declared_size_refuse_before_payload(self) -> None:
        for header in (
            struct.pack(">4sBI", b"BAD!", BridgeFrameType.REQUEST, 1),
            struct.pack(">4sBI", b"HB01", 255, 1),
            struct.pack(">4sBI", b"HB01", BridgeFrameType.REQUEST, 0xFFFFFFFF),
            struct.pack(">4sBI", b"HB01", BridgeFrameType.CANCEL, 1),
        ):
            with self.subTest(header=header):
                decoder = BridgeFrameDecoder()
                with self.assertRaises(BridgeFrameError):
                    decoder.feed(header)
                with self.assertRaisesRegex(BridgeFrameError, "retired"):
                    decoder.feed(b"")

    def test_wire_refusal_has_no_original_header_exception_chain(self) -> None:
        for header in (
            struct.pack(">4sBI", b"HB01", 255, 0),
            struct.pack(">4sBI", b"BAD!", BridgeFrameType.REQUEST, 0),
            struct.pack(">4sBI", b"HB01", BridgeFrameType.REQUEST, 0xFFFFFFFF),
        ):
            with self.subTest(header=header), self.assertRaises(BridgeFrameError) as raised:
                BridgeFrameDecoder().feed(header)
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)

    def test_partial_header_and_body_fail_on_eof(self) -> None:
        wire = encode_bridge_frame(BridgeFrame(BridgeFrameType.REQUEST, b"example-body"))
        for length in (1, 8, 9, len(wire) - 1):
            with self.subTest(length=length):
                decoder = BridgeFrameDecoder()
                decoder.feed(wire[:length])
                with self.assertRaisesRegex(BridgeFrameError, "truncated"):
                    decoder.finish()
                with self.assertRaisesRegex(BridgeFrameError, "retired"):
                    decoder.finish()

    def test_completed_decoder_cannot_accept_a_new_sequence(self) -> None:
        decoder = BridgeFrameDecoder()
        decoder.finish()
        with self.assertRaisesRegex(BridgeFrameError, "retired"):
            decoder.feed(encode_bridge_frame(BridgeFrame(BridgeFrameType.SPEC, b"example")))

    def test_frame_and_byte_budgets_count_empty_frames_and_headers(self) -> None:
        empty = encode_bridge_frame(BridgeFrame(BridgeFrameType.RESPONSE_END, b""))
        decoder = BridgeFrameDecoder(max_frames=2)
        decoder.feed(empty + empty)
        with self.assertRaisesRegex(BridgeFrameError, "budget"):
            decoder.feed(empty)
        decoder = BridgeFrameDecoder(max_bytes=len(empty))
        decoder.feed(empty)
        with self.assertRaisesRegex(BridgeFrameError, "budget"):
            decoder.feed(b"x")

    def test_large_capsule_is_bounded_and_streamed_in_limited_feed_chunks(self) -> None:
        frame = BridgeFrame(BridgeFrameType.CAPSULE, b"x" * (1024 * 1024))
        wire = encode_bridge_frame(frame)
        decoder = BridgeFrameDecoder()
        observed = []
        for offset in range(0, len(wire), 8192):
            observed.extend(decoder.feed(wire[offset : offset + 8192]))
        decoder.finish()
        self.assertEqual(observed, [frame])
        with self.assertRaisesRegex(BridgeFrameError, "feed_bound"):
            BridgeFrameDecoder().feed(wire)

    def test_encoder_and_decoder_reject_invalid_limits_and_payloads(self) -> None:
        for kwargs in (
            {"max_frames": True},
            {"max_frames": 0},
            {"max_frames": 4097},
            {"max_bytes": False},
            {"max_bytes": 0},
            {"max_bytes": 16 * 1024 * 1024 + 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(BridgeFrameError):
                BridgeFrameDecoder(**kwargs)
        for frame in (
            BridgeFrame(BridgeFrameType.CAPSULE, b"x" * (1024 * 1024 + 1)),
            BridgeFrame(BridgeFrameType.RESPONSE_HEADERS, b"x" * (16 * 1024 + 1)),
            BridgeFrame(BridgeFrameType.RESPONSE_CHUNK, b"x" * (64 * 1024 + 1)),
            BridgeFrame(BridgeFrameType.CANCEL, b"x"),
        ):
            with self.subTest(kind=frame.kind), self.assertRaises(BridgeFrameError):
                encode_bridge_frame(frame)

    def test_declared_body_cannot_exceed_remaining_total_budget(self) -> None:
        decoder = BridgeFrameDecoder(max_bytes=10)
        with self.assertRaisesRegex(BridgeFrameError, "byte_budget"):
            decoder.feed(struct.pack(">4sBI", b"HB01", BridgeFrameType.REQUEST, 2))
        with self.assertRaisesRegex(BridgeFrameError, "retired"):
            decoder.feed(b"xx")

    def test_partial_second_frame_counts_previously_consumed_bytes(self) -> None:
        first = encode_bridge_frame(BridgeFrame(BridgeFrameType.RESPONSE_END, b""))
        second = encode_bridge_frame(BridgeFrame(BridgeFrameType.REQUEST, b"xx"))
        decoder = BridgeFrameDecoder(max_bytes=len(first) + len(second) - 1)
        self.assertEqual(len(decoder.feed(first + second[:8])), 1)
        with self.assertRaisesRegex(BridgeFrameError, "byte_budget"):
            decoder.feed(second[8:9])
        with self.assertRaisesRegex(BridgeFrameError, "retired"):
            decoder.feed(second[9:])

    def test_mutable_feed_and_invalid_encoder_objects_refuse_without_data(self) -> None:
        for value in (bytearray(b"example-private"), "example-private", None):
            decoder = BridgeFrameDecoder()
            with (
                self.subTest(value_type=type(value)),
                self.assertRaises(BridgeFrameError) as raised,
            ):
                decoder.feed(cast(Any, value))
            self.assertNotIn("example-private", str(raised.exception))
            with self.assertRaisesRegex(BridgeFrameError, "retired"):
                decoder.feed(b"")
        for frame in (
            BridgeFrame(cast(Any, "example-private"), b""),
            BridgeFrame(BridgeFrameType.REQUEST, cast(Any, bytearray(b"example-private"))),
        ):
            self.assertNotIn("example-private", repr(frame))
            with self.assertRaises(BridgeFrameError) as raised:
                encode_bridge_frame(frame)
            self.assertNotIn("example-private", str(raised.exception))
