"""Actual pipes and fictional HTTP peers; no provider or service calls."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.review_bridge_attempt import BridgeAttemptGate, BridgeAttemptSpec
from hermes_codex_router.review_materials import MaterialSelection, build_review_capsule
from tests.fd_fixture import assert_descriptor_cleanup
from tests.review_bridge_pipe_fixture import PipeFixtureResult, actor_argv, run_pipe_fixture


class ReviewBridgePipeTests(unittest.TestCase):
    def run_case(
        self,
        scenario: str = "success",
        *,
        material_size: int = 26000,
        write_quantum: int = 511,
        pipe_capacity: int = 4096,
        cancel_at: str | None = None,
    ) -> PipeFixtureResult:
        with tempfile.TemporaryDirectory(prefix="example-review-pipe-") as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            content = (b"explicit-fictional-material" * (material_size // 26 + 1))[:material_size]
            selections = []
            for index, offset in enumerate(range(0, len(content), 65536)):
                chunk = content[offset : offset + 65536]
                name = f"visible{index}.txt"
                (root / name).write_bytes(chunk)
                selections.append(
                    MaterialSelection(name, len(chunk), hashlib.sha256(chunk).hexdigest())
                )
            with assert_descriptor_cleanup(self):
                with build_review_capsule(
                    root, tuple(selections), binding="example-result"
                ) as capsule:
                    response = os.urandom(64000)
                    calls: list[bytes] = []

                    def callback(body: bytes) -> bytes:
                        calls.append(body)
                        return response

                    spec = BridgeAttemptSpec(
                        "example-attempt",
                        "example-result",
                        capsule.digest,
                        capsule.size,
                        "00000000-0000-4000-8000-000000000001",
                        "example-model",
                        "high",
                        1024,
                        hashlib.sha256(capsule.read()).hexdigest(),
                    )
                    gate = BridgeAttemptGate(spec, capsule, upstream=callback)
                    result = run_pipe_fixture(
                        actor_argv(sys.executable),
                        {},
                        gate,
                        scenario=scenario,
                        timeout=3
                        if scenario
                        in (
                            "no_read",
                            "flood",
                            "held_pipe",
                            "response_no_read",
                            "slow_header",
                            "slow_body",
                        )
                        else 10,
                        write_quantum=write_quantum,
                        pipe_capacity=pipe_capacity,
                        cancel_at=cancel_at,
                    )
                    self.assertEqual(len(calls), int(gate.observation.attempted))
                    if calls:
                        self.assertEqual(calls, [capsule.read()])
                    if result.success:
                        self.assertEqual(
                            result.receipt["response_sha256"], hashlib.sha256(response).hexdigest()
                        )
                        self.assertEqual(result.receipt["response_size"], len(response))
                    return result

    def test_fragmentation_backpressure_and_delivered_fresh_response(self) -> None:
        result = self.run_case(write_quantum=7, pipe_capacity=4096)
        self.assertTrue(result.success, result.error)

    def test_real_pipe_short_writes_preserve_the_response(self) -> None:
        result = self.run_case(write_quantum=8192, pipe_capacity=4096)
        self.assertTrue(result.success, result.error)
        self.assertGreater(result.short_writes, 0)

    def test_total_material_limit_transits_one_request(self) -> None:
        result = self.run_case(material_size=256 * 1024)
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.request_seen)
        self.assertTrue(result.response_ended)
        self.assertTrue(result.transport_closed)
        self.assertTrue(result.drained)
        self.assertGreater(result.write_calls, 100)

    def test_large_legal_capsule_is_not_mistaken_for_pending_stdout(self) -> None:
        result = self.run_case(material_size=65536)
        self.assertTrue(result.success, result.error)

    def test_duplicate_request_never_repeats_callback(self) -> None:
        result = self.run_case("duplicate_request")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "bridge_sequence_order_invalid")
        self.assertTrue(result.attempted)

    def test_wrong_digest_retires_without_callback(self) -> None:
        result = self.run_case("wrong_digest")
        self.assertFalse(result.success)
        self.assertFalse(result.attempted)
        self.assertEqual(result.error, "bridge_attempt_request_invalid")

    def test_admitted_end_does_not_complete_blocked_response_writer(self) -> None:
        result = self.run_case("response_no_read")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "example_pipe_deadline")
        self.assertTrue(result.attempted)
        self.assertTrue(result.response_ended)
        self.assertFalse(result.drained)
        self.assertTrue(result.cleanup_eof)

    def test_partial_response_abort_closes_instead_of_appending_cancel(self) -> None:
        result = self.run_case(cancel_at="partial_response", write_quantum=3)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "example_partial_abort")
        self.assertTrue(result.attempted)
        self.assertTrue(result.revoked)
        self.assertFalse(result.drained)
        self.assertTrue(result.cleanup_eof)

    def test_drained_end_requires_matching_fresh_client_receipt(self) -> None:
        result = self.run_case("bad_receipt")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "example_receipt_invalid")
        self.assertTrue(result.attempted)
        self.assertTrue(result.drained)
        self.assertTrue(result.response_ended)
        self.assertTrue(result.transport_closed)
        self.assertTrue(result.cleanup_eof)

    def test_extra_http_bytes_cannot_create_second_claim(self) -> None:
        result = self.run_case("extra_request")
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_eof)
        self.assertTrue(
            b"example_http_extra_bytes" in result.stderr
            or b"example_http_repeat_or_eof" in result.stderr,
            result.stderr,
        )
        self.assertNotIn(b"example_actor_deadline", result.stderr)

    def test_delayed_extra_bytes_are_observed_while_sending_response(self) -> None:
        result = self.run_case("delayed_extra_request")
        self.assertFalse(result.success)
        self.assertTrue(result.attempted)
        self.assertIn(b"example_http_repeat_or_eof", result.stderr)
        self.assertNotIn(b"example_actor_deadline", result.stderr)
        self.assertTrue(result.cleanup_eof)
        # Whether TCP delivers the extra bytes before or after the exact body
        # affects the first claim, never permits a second one.

    def test_truncated_frame_and_early_exit_do_not_become_success(self) -> None:
        for scenario in ("truncated", "early_exit"):
            with self.subTest(scenario=scenario):
                result = self.run_case(scenario)
                self.assertFalse(result.success)
                self.assertFalse(result.attempted)

    def test_cancel_before_claim_and_after_claim_keep_consumption_distinct(self) -> None:
        for stage, expected in (("before_claim", False), ("after_claim", True)):
            with self.subTest(stage=stage):
                result = self.run_case(cancel_at=stage)
                self.assertFalse(result.success)
                self.assertEqual(result.attempted, expected)
                self.assertTrue(result.revoked)

    def test_deadline_is_not_extended_by_flood_or_held_pipe(self) -> None:
        for scenario in ("no_read", "flood", "held_pipe"):
            with self.subTest(scenario=scenario):
                result = self.run_case(scenario)
                self.assertFalse(result.success)
                self.assertLess(result.elapsed, 8)
                self.assertLessEqual(result.stdout_bytes, 256 * 1024)

    def test_http_invalid_or_slow_headers_never_claim(self) -> None:
        for scenario in (
            "duplicate_length",
            "transfer_encoding",
            "absolute_uri",
            "connect",
            "slow_header",
            "slow_body",
        ):
            with self.subTest(scenario=scenario):
                result = self.run_case(scenario)
                self.assertFalse(result.success)
                self.assertFalse(result.attempted)


if __name__ == "__main__":
    unittest.main()
