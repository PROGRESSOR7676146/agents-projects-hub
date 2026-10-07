from __future__ import annotations

import unittest
from typing import Any, cast

from hermes_codex_router.review_bridge_protocol import (
    BridgeFrame,
    BridgeFrameType,
    encode_bridge_frame,
)
from hermes_codex_router.review_bridge_sequence import (
    BridgeDirection,
    BridgeSequence,
    BridgeSequenceError,
)
from hermes_codex_router.review_bridge_write_buffer import (
    BridgeWriteBuffer,
    BridgeWriteError,
    bounded_response_frames,
)


class ReviewBridgeWriteBufferTests(unittest.TestCase):
    def frame(self, payload: bytes = b"example-response") -> BridgeFrame:
        return BridgeFrame(BridgeFrameType.RESPONSE_CHUNK, payload)

    def test_partial_writes_and_would_block_preserve_every_wire_byte_in_order(self) -> None:
        frames = bounded_response_frames(b"example-headers", b"example-body" * 10000)
        expected = b"".join(encode_bridge_frame(frame) for frame in frames)
        buffer = BridgeWriteBuffer()
        for frame in frames:
            self.assertTrue(buffer.enqueue(frame))
        observed = bytearray()
        while offered := buffer.peek(max_bytes=17):
            self.assertEqual(offered, buffer.peek(max_bytes=17))
            buffer.advance(0)  # fictional would-block; no bytes were written
            self.assertEqual(offered, buffer.peek(max_bytes=17))
            count = min(3, len(offered))
            observed.extend(offered[:count])
            buffer.advance(count)
        self.assertEqual(bytes(observed), expected)
        self.assertEqual(buffer.observation.pending_bytes, 0)
        self.assertEqual(buffer.observation.advanced_bytes, len(expected))

    def test_full_queue_rejects_admission_without_losing_pending_or_counting_retry(self) -> None:
        frame = self.frame(b"x")
        wire = encode_bridge_frame(frame)
        buffer = BridgeWriteBuffer(capacity=len(wire))
        self.assertTrue(buffer.enqueue(frame))
        self.assertFalse(buffer.enqueue(frame))
        self.assertEqual(buffer.observation.admitted_frames, 1)
        self.assertEqual(buffer.peek(), wire)
        buffer.advance(len(wire))
        self.assertTrue(buffer.enqueue(frame))
        self.assertEqual(buffer.observation.admitted_frames, 2)
        self.assertEqual(buffer.peek(), wire)

    def test_serialized_fake_owner_advances_sequence_only_after_buffer_admission(self) -> None:
        host, child = BridgeDirection.HOST_TO_CHILD, BridgeDirection.CHILD_TO_HOST
        sequence = BridgeSequence()
        for direction, kind in (
            (host, BridgeFrameType.SPEC),
            (host, BridgeFrameType.CAPSULE),
            (child, BridgeFrameType.REQUEST),
        ):
            sequence.observe(direction, BridgeFrame(kind, b""))
        buffer = BridgeWriteBuffer(capacity=10)

        def admit(frame: BridgeFrame) -> bool:
            if not buffer.enqueue(frame):
                return False
            # No physical write may occur between admission and ordering.
            try:
                sequence.observe(host, frame)
            except Exception:
                buffer.cancel()
                raise
            return True

        self.assertTrue(admit(BridgeFrame(BridgeFrameType.RESPONSE_HEADERS, b"x")))
        chunk = BridgeFrame(BridgeFrameType.RESPONSE_CHUNK, b"x")
        self.assertFalse(admit(chunk))
        self.assertEqual(buffer.observation.admitted_frames, 1)
        buffer.advance(len(buffer.peek()))
        self.assertTrue(admit(chunk))
        self.assertEqual(buffer.observation.admitted_frames, 2)
        buffer.advance(len(buffer.peek()))
        self.assertTrue(admit(BridgeFrame(BridgeFrameType.RESPONSE_END, b"")))
        self.assertTrue(sequence.observation.response_ended)
        self.assertEqual(buffer.observation.admitted_frames, 3)
        buffer.advance(len(buffer.peek()))
        advanced = buffer.observation.advanced_bytes
        with self.assertRaises(BridgeSequenceError):
            admit(BridgeFrame(BridgeFrameType.RESPONSE_HEADERS, b""))
        self.assertTrue(sequence.observation.failed)
        self.assertEqual(buffer.observation.pending_bytes, 0)
        self.assertEqual(buffer.observation.advanced_bytes, advanced)
        self.assertTrue(buffer.observation.cancelled)
        with self.assertRaisesRegex(BridgeWriteError, "retired"):
            buffer.enqueue(chunk)

    def test_partial_send_frees_capacity_without_reordering_frame_tail(self) -> None:
        frame = self.frame(b"xx")
        wire = encode_bridge_frame(frame)
        buffer = BridgeWriteBuffer(capacity=len(wire) + 4)
        self.assertTrue(buffer.enqueue(frame))
        buffer.peek()
        buffer.advance(len(wire) - 4)
        self.assertTrue(buffer.enqueue(frame))
        self.assertEqual(buffer.peek(), wire[-4:])
        buffer.advance(4)
        self.assertEqual(buffer.peek(), wire)

    def test_cancel_under_backpressure_discards_only_pending_and_never_rearms(self) -> None:
        frame = self.frame()
        wire = encode_bridge_frame(frame)
        buffer = BridgeWriteBuffer(capacity=len(wire))
        buffer.enqueue(frame)
        buffer.peek()
        buffer.advance(5)
        self.assertFalse(buffer.enqueue(frame))
        buffer.cancel()
        self.assertEqual(buffer.observation.advanced_bytes, 5)
        self.assertEqual(buffer.observation.pending_bytes, 0)
        self.assertTrue(buffer.observation.cancelled)
        for operation in (
            lambda: buffer.peek(),
            lambda: buffer.advance(1),
            lambda: buffer.enqueue(frame),
        ):
            with self.assertRaisesRegex(BridgeWriteError, "retired"):
                operation()

    def test_abort_mid_header_or_payload_cannot_append_wire_cancel_to_truncated_frame(self) -> None:
        for count in (3, 11):
            buffer = BridgeWriteBuffer()
            buffer.enqueue(self.frame())
            buffer.peek()
            buffer.advance(count)
            buffer.cancel()
            self.assertEqual(buffer.observation.advanced_bytes, count)
            with self.subTest(count=count), self.assertRaisesRegex(BridgeWriteError, "retired"):
                buffer.enqueue(BridgeFrame(BridgeFrameType.CANCEL, b""))

    def test_advance_requires_exact_integer_within_last_offer(self) -> None:
        for count in (True, -1, 4, "example-private"):
            buffer = BridgeWriteBuffer()
            buffer.enqueue(self.frame())
            buffer.peek(max_bytes=3)
            with self.subTest(count=count), self.assertRaises(BridgeWriteError) as raised:
                buffer.advance(cast(Any, count))
            self.assertNotIn("example-private", repr(raised.exception))
            self.assertTrue(buffer.observation.failed)
            self.assertEqual(buffer.observation.advanced_bytes, 0)
        buffer = BridgeWriteBuffer()
        buffer.enqueue(self.frame())
        with self.assertRaises(BridgeWriteError):
            buffer.advance(1)

    def test_outstanding_offer_is_stable_until_positive_progress(self) -> None:
        buffer = BridgeWriteBuffer()
        buffer.enqueue(self.frame())
        original = buffer.peek(max_bytes=3)
        self.assertEqual(buffer.peek(max_bytes=20), original)
        buffer.advance(0)
        self.assertEqual(buffer.peek(max_bytes=20), original)
        buffer.advance(1)
        self.assertNotEqual(buffer.peek(max_bytes=20), original)
        with self.assertRaisesRegex(BridgeWriteError, "offer_inflight"):
            buffer.peek(max_bytes=1)

    def test_frame_larger_than_capacity_and_lifetime_bounds_permanently_refuse(self) -> None:
        buffer = BridgeWriteBuffer(capacity=9)
        with self.assertRaisesRegex(BridgeWriteError, "capacity"):
            buffer.enqueue(self.frame(b"x"))
        for kwargs in ({"max_frames": 1}, {"max_bytes": 10}):
            buffer = BridgeWriteBuffer(**kwargs)
            buffer.enqueue(self.frame(b"x"))
            buffer.advance(len(buffer.peek()))
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(BridgeWriteError, "budget"):
                buffer.enqueue(self.frame(b"x"))
            self.assertEqual(buffer.observation.advanced_bytes, 10)

    def test_response_tuple_is_finite_bounded_and_payload_opaque(self) -> None:
        for size in (0, 1, 65536, 65537, 1024 * 1024):
            frames = bounded_response_frames(b"example-headers", b"x" * size)
            self.assertEqual(frames[0].kind, BridgeFrameType.RESPONSE_HEADERS)
            self.assertEqual(frames[-1].kind, BridgeFrameType.RESPONSE_END)
            self.assertLessEqual(len(frames), 18)
            self.assertEqual(b"".join(frame.payload for frame in frames[1:-1]), b"x" * size)
            self.assertTrue(all(len(frame.payload) <= 65536 for frame in frames[1:-1]))
        for headers, body in (
            (b"x" * 16385, b""),
            (b"", b"x" * (1024 * 1024 + 1)),
            (b"", bytearray(b"example-private")),
        ):
            with self.assertRaises(BridgeWriteError) as raised:
                bounded_response_frames(headers, cast(Any, body))
            self.assertNotIn("example-private", repr(raised.exception))

    def test_invalid_budget_and_peek_size_do_not_accept_mutable_or_unbounded_inputs(self) -> None:
        for kwargs in ({"capacity": True}, {"capacity": 0}, {"max_frames": 4097}, {"max_bytes": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(BridgeWriteError):
                BridgeWriteBuffer(**kwargs)
        for size in (0, True, 65537):
            buffer = BridgeWriteBuffer()
            with self.subTest(size=size), self.assertRaises(BridgeWriteError):
                buffer.peek(max_bytes=size)
