"""One local fake callback, never durable or native authorization evidence."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import threading
import unittest
import uuid
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router.review_bridge_attempt import (
    BridgeAttemptError,
    BridgeAttemptGate,
    BridgeAttemptSpec,
    BridgeAttemptState,
)
from hermes_codex_router.review_bridge_protocol import BridgeFrame, BridgeFrameType
from hermes_codex_router.review_materials import (
    MaterialSelection,
    ReviewCapsule,
    _create_sealable_memfd,
    build_review_capsule,
)


class ReviewBridgeAttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-review-attempt-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "visible.txt"
        data = b"example-selected-material"
        self.source.write_bytes(data)
        self.capsule = build_review_capsule(
            root,
            (MaterialSelection("visible.txt", len(data), hashlib.sha256(data).hexdigest()),),
            binding="example-result",
        )
        self.addCleanup(self.capsule.close)
        self.body = b'{"model":"example-model","question":"example-review"}'
        self.spec = BridgeAttemptSpec(
            attempt_id="example-attempt",
            material_binding="example-result",
            capsule_sha256=self.capsule.digest,
            capsule_size=self.capsule.size,
            native_session_id="00000000-0000-4000-8000-000000000001",
            model="example-model",
            effort="high",
            max_output_tokens=1024,
            expected_request_sha256=hashlib.sha256(self.body).hexdigest(),
        )
        self.now = 10.0
        self.calls: list[bytes] = []

    def upstream(self, body: bytes) -> bytes:
        self.calls.append(body)
        return b"example-fake-response"

    def gate(self, **kwargs: Any) -> BridgeAttemptGate:
        return BridgeAttemptGate(
            kwargs.pop("spec", self.spec),
            self.capsule,
            upstream=kwargs.pop("upstream", self.upstream),
            clock=kwargs.pop("clock", lambda: self.now),
            **kwargs,
        )

    def request(self, body: bytes | None = None) -> BridgeFrame:
        return BridgeFrame(BridgeFrameType.REQUEST, self.body if body is None else body)

    def test_exact_trusted_request_calls_once_and_preserves_sealed_material_bytes(self) -> None:
        original = self.capsule.read()
        gate = self.gate()
        self.source.write_bytes(b"later-source-change")
        self.capsule.close()
        self.assertEqual(gate.capsule_bytes, original)
        response = gate.submit(self.request())
        self.assertEqual(response, b"example-fake-response")
        self.assertEqual(self.calls, [self.body])
        self.assertEqual(gate.observation.state, BridgeAttemptState.CALLBACK_RETURNED)
        self.assertTrue(gate.observation.attempted)
        with self.assertRaisesRegex(BridgeAttemptError, "retired"):
            gate.submit(self.request())
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("example-selected-material", repr(gate))
        with self.assertRaises(FrozenInstanceError):
            self.spec.model = "changed"  # type: ignore[misc]

    def test_capsule_digest_binding_size_and_closed_capsule_refuse_before_call(self) -> None:
        for change in (
            {"capsule_sha256": "0" * 64},
            {"material_binding": "example-other"},
            {"capsule_size": self.capsule.size + 1},
        ):
            with self.subTest(change=change), self.assertRaises(BridgeAttemptError):
                self.gate(spec=replace(self.spec, **change))
        self.capsule.close()
        with self.assertRaises(BridgeAttemptError):
            self.gate()
        self.assertEqual(self.calls, [])

    def test_changed_body_with_correct_capsule_identity_closes_without_call(self) -> None:
        gate = self.gate()
        with self.assertRaisesRegex(BridgeAttemptError, "request"):
            gate.submit(self.request(b'{"model":"example-other","question":"changed"}'))
        with self.assertRaisesRegex(BridgeAttemptError, "retired"):
            gate.submit(self.request())
        self.assertEqual(self.calls, [])
        self.assertEqual(gate.observation.state, BridgeAttemptState.CLOSED)
        self.assertFalse(gate.observation.attempted)

    def test_unsealed_capsule_refuses_and_caller_retains_descriptor_ownership(self) -> None:
        data = self.capsule.read()
        descriptor = _create_sealable_memfd()
        with ReviewCapsule(descriptor, self.capsule.digest, len(data)) as unsealed:
            self.assertEqual(os.write(descriptor, data), len(data))
            with self.assertRaisesRegex(BridgeAttemptError, "material"):
                BridgeAttemptGate(
                    self.spec, unsealed, upstream=self.upstream, clock=lambda: self.now
                )
            self.assertEqual(os.fstat(descriptor).st_size, len(data))
        self.assertEqual(self.calls, [])

    def test_non_capsule_and_overridden_capsule_read_are_refused_before_any_read(self) -> None:
        reads: list[bool] = []
        data, digest, size = self.capsule.read(), self.capsule.digest, self.capsule.size

        class DuckCapsule:
            def read(self) -> bytes:
                reads.append(True)
                return data

        class OverriddenCapsule(ReviewCapsule):
            def read(self) -> bytes:
                reads.append(True)
                return data

        for capsule in (None, DuckCapsule(), OverriddenCapsule(-1, digest, size)):
            with (
                self.subTest(capsule_type=type(capsule)),
                self.assertRaisesRegex(BridgeAttemptError, "capsule_type"),
            ):
                BridgeAttemptGate(self.spec, capsule, upstream=self.upstream)  # type: ignore[arg-type]
        self.assertEqual(reads, [])
        self.assertEqual(self.calls, [])

    def test_oversize_request_and_bad_or_backward_clock_close_before_call(self) -> None:
        gate = self.gate()
        with self.assertRaisesRegex(BridgeAttemptError, "request"):
            gate.submit(self.request(b"x" * (1024 * 1024 + 1)))
        for clock in (math.nan, math.inf, True, 9.0):
            self.now = 10
            gate = self.gate()
            self.now = clock
            with self.subTest(clock=clock), self.assertRaises(BridgeAttemptError):
                gate.submit(self.request())
            self.assertEqual(gate.observation.state, BridgeAttemptState.CLOSED)
            gate.close()
            with self.assertRaisesRegex(BridgeAttemptError, "retired"):
                gate.submit(self.request())
        self.assertEqual(self.calls, [])

    def test_child_frames_cannot_set_host_spec_capsule_response_or_policy(self) -> None:
        for kind in BridgeFrameType:
            if kind == BridgeFrameType.REQUEST:
                continue
            gate = self.gate()
            with self.subTest(kind=kind), self.assertRaises(BridgeAttemptError):
                gate.submit(BridgeFrame(kind, b""))
            with self.assertRaisesRegex(BridgeAttemptError, "retired"):
                gate.submit(self.request())
        self.assertEqual(self.calls, [])

    def test_concurrent_and_reentrant_submissions_never_call_twice(self) -> None:
        ready = threading.Barrier(3)
        entered, release = threading.Event(), threading.Event()

        def blocked(body: bytes) -> bytes:
            self.calls.append(body)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("example-fixture-timeout")
            return b"example-response"

        gate = self.gate(upstream=blocked)
        outcomes: list[object] = []

        def run() -> None:
            ready.wait(5)
            try:
                outcomes.append(gate.submit(self.request()))
            except BridgeAttemptError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            ready.wait(5)
            self.assertTrue(entered.wait(5))
            self.assertEqual(gate.observation.state, BridgeAttemptState.CONSUMED)
        finally:
            release.set()
            for thread in threads:
                thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(self.calls, [self.body])
        self.assertEqual(sum(isinstance(result, bytes) for result in outcomes), 1)
        self.assertEqual(sum(isinstance(result, BridgeAttemptError) for result in outcomes), 1)

        def recursive(body: bytes) -> bytes:
            self.calls.append(body)
            with self.assertRaisesRegex(BridgeAttemptError, "retired"):
                recursive_gate.submit(self.request())
            return b"example-response"

        recursive_gate = self.gate(upstream=recursive)
        self.assertEqual(recursive_gate.submit(self.request()), b"example-response")
        self.assertEqual(len(self.calls), 2)

    def test_callback_exception_is_uncertain_consumed_and_hides_diagnostic(self) -> None:
        def failing(body: bytes) -> bytes:
            self.calls.append(body)
            raise RuntimeError("example-private-callback-secret")

        gate = self.gate(upstream=failing)
        with self.assertRaises(BridgeAttemptError) as raised:
            gate.submit(self.request())
        self.assertNotIn("example-private", str(raised.exception))
        self.assertNotIn("example-private", repr(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(gate.observation.state, BridgeAttemptState.UNCERTAIN)
        self.assertTrue(gate.observation.attempted)
        with self.assertRaisesRegex(BridgeAttemptError, "retired"):
            gate.submit(self.request())
        self.assertEqual(self.calls, [self.body])

    def test_cancel_and_exact_deadline_before_claim_mean_zero_calls(self) -> None:
        gate = self.gate(timeout_seconds=5)
        gate.cancel()
        with self.assertRaisesRegex(BridgeAttemptError, "retired"):
            gate.submit(self.request())
        self.assertFalse(gate.observation.attempted)
        gate = self.gate(timeout_seconds=5)
        self.now = 15
        with self.assertRaisesRegex(BridgeAttemptError, "expired"):
            gate.submit(self.request())
        self.assertEqual(self.calls, [])
        self.assertEqual(gate.observation.state, BridgeAttemptState.CLOSED)

    def test_cancel_or_expiry_after_claim_does_not_claim_no_call(self) -> None:
        for expired in (False, True):
            self.now = 10

            def revoke(body: bytes) -> bytes:
                self.calls.append(body)
                if expired:
                    self.now = 15
                else:
                    gate.cancel()
                return b"example-response"

            gate = self.gate(upstream=revoke, timeout_seconds=5)
            with self.subTest(expired=expired), self.assertRaises(BridgeAttemptError):
                gate.submit(self.request())
            self.assertTrue(gate.observation.attempted)
            self.assertEqual(gate.observation.state, BridgeAttemptState.UNCERTAIN)
            with self.assertRaisesRegex(BridgeAttemptError, "retired"):
                gate.submit(self.request())
        self.assertEqual(len(self.calls), 2)

    def test_invalid_host_spec_clock_timeout_and_mutable_output_refuse(self) -> None:
        for change in (
            {"capsule_size": True},
            {"max_output_tokens": False},
            {"capsule_sha256": "wrong"},
            {"expected_request_sha256": "wrong"},
            {"native_session_id": "not-uuid"},
            {"model": "example model"},
            {"effort": "unknown"},
            {"attempt_id": "../example"},
        ):
            with self.subTest(change=change), self.assertRaises(BridgeAttemptError):
                self.gate(spec=replace(self.spec, **change))
        for value in (True, math.nan, math.inf, -1):
            with self.subTest(clock=value), self.assertRaises(BridgeAttemptError):
                self.gate(clock=lambda: value)
        for value in (True, math.nan, math.inf, 0, -1, 301):
            with self.subTest(timeout=value), self.assertRaises(BridgeAttemptError):
                self.gate(timeout_seconds=value)
        self.assertEqual(self.calls, [])
        for response in (bytearray(b"example-private-output"), b"x" * (64 * 1024 + 1)):
            gate = self.gate(upstream=lambda body: response)
            with self.subTest(output_type=type(response)), self.assertRaises(BridgeAttemptError):
                gate.submit(self.request())
            self.assertTrue(gate.observation.attempted)
            self.assertEqual(gate.observation.state, BridgeAttemptState.UNCERTAIN)

    def test_long_native_identity_refuses_before_uuid_parser(self) -> None:
        calls: list[bool] = []
        native_uuid = uuid.UUID

        def record(value: str) -> uuid.UUID:
            calls.append(True)
            return native_uuid(value)

        with patch("hermes_codex_router.review_bridge_attempt.uuid.UUID", side_effect=record):
            with self.assertRaises(BridgeAttemptError):
                self.gate(spec=replace(self.spec, native_session_id="a" * (1024 * 1024)))
            self.assertEqual(len(calls), 0)
        self.assertEqual(self.calls, [])

    def test_clock_exception_at_admission_or_after_call_is_sanitized_and_not_replayable(
        self,
    ) -> None:
        for after_call in (False, True):
            clock_calls = 0

            def clock() -> float:
                nonlocal clock_calls
                clock_calls += 1
                if clock_calls == (3 if after_call else 2):
                    raise RuntimeError("example-private-clock-error")
                return self.now

            gate = self.gate(clock=clock)
            with (
                self.subTest(after_call=after_call),
                self.assertRaises(BridgeAttemptError) as raised,
            ):
                gate.submit(self.request())
            self.assertNotIn("example-private", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)
            self.assertEqual(gate.observation.attempted, after_call)
            self.assertEqual(
                gate.observation.state,
                (BridgeAttemptState.UNCERTAIN if after_call else BridgeAttemptState.CLOSED),
            )
            gate.close()
            with self.assertRaisesRegex(BridgeAttemptError, "retired"):
                gate.submit(self.request())
        self.assertEqual(self.calls, [self.body])

    def test_other_thread_cancel_does_not_wait_for_callback_or_claim_remote_cancellation(
        self,
    ) -> None:
        entered, release = threading.Event(), threading.Event()
        outcome: list[object] = []

        def blocked(body: bytes) -> bytes:
            self.calls.append(body)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("example-fixture-timeout")
            return b"example-response"

        gate = self.gate(upstream=blocked)

        def submit() -> None:
            try:
                outcome.append(gate.submit(self.request()))
            except BridgeAttemptError as error:
                outcome.append(error)

        thread = threading.Thread(target=submit)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            gate.cancel()
            self.assertTrue(thread.is_alive())
            self.assertTrue(gate.observation.attempted)
            self.assertTrue(gate.observation.revoked)
            self.assertEqual(gate.observation.state, BridgeAttemptState.CONSUMED)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.calls, [self.body])
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], BridgeAttemptError)
        self.assertEqual(gate.observation.state, BridgeAttemptState.UNCERTAIN)
