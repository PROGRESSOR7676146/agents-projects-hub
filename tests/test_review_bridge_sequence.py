from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.review_bridge_attempt import (
    BridgeAttemptError,
    BridgeAttemptGate,
    BridgeAttemptSpec,
)
from hermes_codex_router.review_bridge_protocol import BridgeFrame, BridgeFrameType
from hermes_codex_router.review_bridge_sequence import (
    BridgeDirection,
    BridgeDisposition,
    BridgeSequence,
    BridgeSequenceError,
)
from hermes_codex_router.review_materials import MaterialSelection, build_review_capsule

HOST = BridgeDirection.HOST_TO_CHILD
CHILD = BridgeDirection.CHILD_TO_HOST


class ReviewBridgeSequenceTests(unittest.TestCase):
    def frame(self, kind: BridgeFrameType, payload: bytes = b"") -> BridgeFrame:
        return BridgeFrame(kind, payload)

    def prepared(self, **kwargs: Any) -> BridgeSequence:
        sequence = BridgeSequence(**kwargs)
        sequence.observe(HOST, self.frame(BridgeFrameType.SPEC, b"example-spec"))
        sequence.observe(HOST, self.frame(BridgeFrameType.CAPSULE, b"example-capsule"))
        return sequence

    def requested(self, **kwargs: Any) -> BridgeSequence:
        sequence = self.prepared(**kwargs)
        sequence.observe(CHILD, self.frame(BridgeFrameType.REQUEST, b"example-request"))
        return sequence

    def assert_retired(self, sequence: BridgeSequence) -> None:
        with self.assertRaisesRegex(BridgeSequenceError, "retired"):
            sequence.observe(HOST, self.frame(BridgeFrameType.CANCEL))

    def test_interleaved_native_output_and_response_preserve_transport_only_evidence(self) -> None:
        sequence = self.prepared()
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT, b"example-startup"))
        sequence.observe(CHILD, self.frame(BridgeFrameType.REQUEST, b"example-request"))
        sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS, b"example-headers"))
        sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_CHUNK, b"example-response"))
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT, b"example-visible"))
        sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_END))
        sequence.finish(HOST)
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT, b"example-terminal"))
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT, b"example-exit"))
        sequence.finish(CHILD)
        observation = sequence.observation
        self.assertTrue(observation.request_seen)
        self.assertTrue(observation.transport_closed)
        self.assertFalse(observation.failed)
        self.assertNotIn("example-visible", repr(observation))

    def test_wrong_direction_and_early_child_frames_permanently_refuse(self) -> None:
        for direction, kind in (
            (HOST, BridgeFrameType.REQUEST),
            (HOST, BridgeFrameType.NATIVE_STDOUT),
            (HOST, BridgeFrameType.NATIVE_EXIT),
            (CHILD, BridgeFrameType.SPEC),
            (CHILD, BridgeFrameType.CAPSULE),
            (CHILD, BridgeFrameType.RESPONSE_HEADERS),
            (CHILD, BridgeFrameType.RESPONSE_CHUNK),
            (CHILD, BridgeFrameType.RESPONSE_END),
            (CHILD, BridgeFrameType.CANCEL),
            (CHILD, BridgeFrameType.REQUEST),
            (CHILD, BridgeFrameType.NATIVE_STDOUT),
            (CHILD, BridgeFrameType.NATIVE_EXIT),
        ):
            sequence = BridgeSequence()
            with (
                self.subTest(direction=direction, kind=kind),
                self.assertRaises(BridgeSequenceError),
            ):
                sequence.observe(direction, self.frame(kind))
            self.assertTrue(sequence.observation.failed)
            self.assertFalse(sequence.observation.request_seen)
            self.assert_retired(sequence)

    def test_initialization_order_and_duplicates_refuse(self) -> None:
        for first, second in (
            (None, BridgeFrameType.CAPSULE),
            (BridgeFrameType.SPEC, BridgeFrameType.SPEC),
            (BridgeFrameType.SPEC, BridgeFrameType.RESPONSE_HEADERS),
        ):
            sequence = BridgeSequence()
            if first is not None:
                sequence.observe(HOST, self.frame(first))
            with self.subTest(first=first, second=second), self.assertRaises(BridgeSequenceError):
                sequence.observe(HOST, self.frame(second))
            self.assert_retired(sequence)
        sequence = self.prepared()
        with self.assertRaises(BridgeSequenceError):
            sequence.observe(HOST, self.frame(BridgeFrameType.CAPSULE))

    def test_duplicate_request_and_out_of_order_response_leave_request_seen_sticky(self) -> None:
        for direction, kind in (
            (CHILD, BridgeFrameType.REQUEST),
            (HOST, BridgeFrameType.RESPONSE_CHUNK),
            (HOST, BridgeFrameType.RESPONSE_END),
        ):
            sequence = self.requested()
            with self.subTest(kind=kind), self.assertRaises(BridgeSequenceError):
                sequence.observe(direction, self.frame(kind))
            self.assertTrue(sequence.observation.request_seen)
            self.assert_retired(sequence)
        sequence = self.requested()
        sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS))
        with self.assertRaises(BridgeSequenceError):
            sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS))

    def test_response_end_and_native_exit_are_each_single_use(self) -> None:
        for late in (BridgeFrameType.RESPONSE_CHUNK, BridgeFrameType.RESPONSE_END):
            sequence = self.requested()
            sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS))
            sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_END))
            with self.subTest(late=late), self.assertRaises(BridgeSequenceError):
                sequence.observe(HOST, self.frame(late))
            self.assertTrue(sequence.observation.request_seen)
        sequence = self.prepared()
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT))
        with self.assertRaises(BridgeSequenceError):
            sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT))

    def test_early_exit_is_transport_refusal_without_request_or_completion_claim(self) -> None:
        sequence = self.prepared()
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT, b"example-refusal"))
        sequence.finish(CHILD)
        sequence.finish(HOST)
        self.assertFalse(sequence.observation.request_seen)
        self.assertTrue(sequence.observation.transport_closed)
        self.assertFalse(sequence.observation.response_ended)

    def test_cancel_drains_inflight_output_without_publication_or_erasing_prior_request(
        self,
    ) -> None:
        sequence = self.requested()
        sequence.observe(HOST, self.frame(BridgeFrameType.CANCEL))
        sequence.finish(HOST)
        disposition = sequence.observe(
            CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT, b"example-inflight-output")
        )
        self.assertEqual(disposition, BridgeDisposition.DISCARD_STDOUT)
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT))
        sequence.finish(CHILD)
        self.assertTrue(sequence.observation.cancelled)
        self.assertTrue(sequence.observation.request_seen)
        self.assertTrue(sequence.observation.transport_closed)
        for direction, late in ((HOST, BridgeFrameType.CANCEL), (CHILD, BridgeFrameType.REQUEST)):
            sequence = self.prepared()
            sequence.observe(HOST, self.frame(BridgeFrameType.CANCEL))
            with self.subTest(late=late), self.assertRaises(BridgeSequenceError):
                sequence.observe(direction, self.frame(late))
        sequence = BridgeSequence()
        sequence.observe(HOST, self.frame(BridgeFrameType.CANCEL))
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT))
        self.assertFalse(sequence.observation.request_seen)

    def test_exit_after_request_without_response_is_incomplete_even_with_both_eofs(self) -> None:
        sequence = self.requested()
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT, b"example-failure"))
        sequence.finish(CHILD)
        sequence.finish(HOST)
        self.assertTrue(sequence.observation.request_seen)
        self.assertTrue(sequence.observation.transport_closed)
        self.assertTrue(sequence.observation.response_incomplete)
        self.assertFalse(sequence.observation.response_ended)

    def test_cancelled_stdout_drain_keeps_its_original_stream_budget(self) -> None:
        sequence = self.prepared()
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT, b"x" * 65536))
        sequence.observe(HOST, self.frame(BridgeFrameType.CANCEL))
        for _ in range(3):
            self.assertEqual(
                sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT, b"x" * 65536)),
                BridgeDisposition.DISCARD_STDOUT,
            )
        with self.assertRaisesRegex(BridgeSequenceError, "stream_budget"):
            sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT, b"x"))

    def test_eof_in_every_unfinished_phase_is_failure_without_rearming(self) -> None:
        for stage in range(6):
            for direction in (HOST, CHILD):
                sequence = BridgeSequence()
                events = (
                    (HOST, self.frame(BridgeFrameType.SPEC)),
                    (HOST, self.frame(BridgeFrameType.CAPSULE)),
                    (CHILD, self.frame(BridgeFrameType.REQUEST)),
                    (HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS)),
                    (HOST, self.frame(BridgeFrameType.RESPONSE_CHUNK)),
                )
                for source, frame in events[:stage]:
                    sequence.observe(source, frame)
                with (
                    self.subTest(stage=stage, direction=direction),
                    self.assertRaises(BridgeSequenceError),
                ):
                    sequence.finish(direction)
                self.assertEqual(sequence.observation.request_seen, stage >= 3)
                self.assert_retired(sequence)

    def test_response_and_stdout_have_separate_aggregate_budgets(self) -> None:
        for kind, direction, count in (
            (BridgeFrameType.RESPONSE_CHUNK, HOST, 16),
            (BridgeFrameType.NATIVE_STDOUT, CHILD, 4),
        ):
            sequence = self.requested()
            sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS))
            for _ in range(count):
                sequence.observe(direction, self.frame(kind, b"x" * 65536))
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(BridgeSequenceError, "stream_budget"),
            ):
                sequence.observe(direction, self.frame(kind, b"x"))
            self.assertTrue(sequence.observation.request_seen)

    def test_direction_budgets_include_headers_and_empty_frames(self) -> None:
        sequence = self.prepared(max_frames=2)
        sequence.observe(CHILD, self.frame(BridgeFrameType.REQUEST))
        sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_STDOUT))
        with self.assertRaisesRegex(BridgeSequenceError, "frame_budget"):
            sequence.observe(CHILD, self.frame(BridgeFrameType.NATIVE_EXIT))
        sequence = BridgeSequence(max_bytes=18)
        sequence.observe(HOST, self.frame(BridgeFrameType.SPEC))
        sequence.observe(HOST, self.frame(BridgeFrameType.CAPSULE))
        sequence.observe(CHILD, self.frame(BridgeFrameType.REQUEST))
        with self.assertRaisesRegex(BridgeSequenceError, "byte_budget"):
            sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS))

    def test_late_sequence_failure_cannot_rearm_the_consumed_fake_callback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-review-sequence-") as temporary:
            root = Path(temporary)
            data = b"example-selected"
            (root / "visible.txt").write_bytes(data)
            selection = MaterialSelection(
                "visible.txt", len(data), hashlib.sha256(data).hexdigest()
            )
            with build_review_capsule(root, (selection,), binding="example-result") as capsule:
                body = b"example-request"
                calls: list[bytes] = []

                def fake_upstream(request: bytes) -> bytes:
                    calls.append(request)
                    return b"example-response"

                spec = BridgeAttemptSpec(
                    attempt_id="example-attempt",
                    material_binding="example-result",
                    capsule_sha256=capsule.digest,
                    capsule_size=capsule.size,
                    native_session_id="00000000-0000-4000-8000-000000000001",
                    model="example-model",
                    effort="high",
                    max_output_tokens=1024,
                    expected_request_sha256=hashlib.sha256(body).hexdigest(),
                )
                gate = BridgeAttemptGate(spec, capsule, upstream=fake_upstream)
                sequence = self.prepared()
                request = self.frame(BridgeFrameType.REQUEST, body)
                sequence.observe(CHILD, request)
                gate.submit(request)
                with self.assertRaises(BridgeSequenceError):
                    sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_CHUNK))
                self.assertTrue(sequence.observation.failed)
                self.assertTrue(sequence.observation.request_seen)
                self.assertTrue(gate.observation.attempted)
                with self.assertRaisesRegex(BridgeAttemptError, "retired"):
                    gate.submit(request)
                self.assertEqual(calls, [body])

    def test_invalid_objects_and_closed_direction_refuse_without_payload_diagnostics(self) -> None:
        sequence = self.requested()
        with self.assertRaises(BridgeSequenceError) as raised:
            sequence.observe(
                cast(Any, "example-private"), self.frame(BridgeFrameType.RESPONSE_HEADERS)
            )
        self.assertNotIn("example-private", repr(raised.exception))
        sequence = self.requested()
        sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_HEADERS))
        sequence.observe(HOST, self.frame(BridgeFrameType.RESPONSE_END))
        sequence.finish(HOST)
        with self.assertRaisesRegex(BridgeSequenceError, "closed"):
            sequence.observe(HOST, self.frame(BridgeFrameType.CANCEL))
