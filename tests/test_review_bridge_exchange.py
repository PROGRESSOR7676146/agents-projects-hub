"""Owned raw exchange evidence, independent of fixture receipt interpretation."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from hermes_codex_router.review_bridge_attempt import BridgeAttemptError
from hermes_codex_router.review_bridge_protocol import BridgeFrame
from tests.claude_native_pipe_contract import NativePipeAttempt, native_pipe_prompt
from tests.claude_native_request_contract import ExpectedNativeRequest
from tests.fd_fixture import assert_descriptor_cleanup
from tests.review_bridge_pipe_fixture import _run_pipe_exchange, actor_argv, run_pipe_fixture
from tests.test_claude_native_pipe_contract import capsule_bytes
from tests.test_claude_native_request_contract import EXAMPLE_ENVIRONMENT


@dataclass(frozen=True)
class Observation:
    attempted: bool
    revoked: bool


class PreparedAttempt:
    """Structural test owner: no production enum, callback or semantic policy."""

    def __init__(self, response: bytes) -> None:
        self.capsule_bytes = b"example-raw-exchange-material"
        self.response = response
        self.attempted = self.revoked = False
        self.closes = 0

    @property
    def observation(self) -> Observation:
        return Observation(self.attempted, self.revoked)

    def submit(self, frame: BridgeFrame) -> bytes:
        if self.attempted or self.revoked or frame.payload != self.capsule_bytes:
            raise BridgeAttemptError("bridge_attempt_request_invalid")
        self.attempted = True
        return self.response

    def cancel(self) -> None:
        self.revoked = True

    def close(self) -> None:
        self.closes += 1
        self.cancel()


class ReviewBridgeExchangeTests(unittest.TestCase):
    def test_raw_exchange_completes_cleanup_before_receipt_interpretation(self) -> None:
        response = os.urandom(1000)
        owner = PreparedAttempt(response)
        with assert_descriptor_cleanup(self):
            exchange = _run_pipe_exchange(actor_argv(sys.executable), {}, owner, timeout=3)
        result = exchange.result
        self.assertTrue(exchange.complete, result.error)
        self.assertTrue(result.cleanup_eof)
        self.assertTrue(result.transport_closed)
        self.assertTrue(result.drained)
        self.assertTrue(result.attempted)
        self.assertFalse(result.revoked)
        self.assertFalse(result.success)
        self.assertEqual(result.receipt, {})
        self.assertEqual(owner.closes, 1)
        self.assertTrue(owner.observation.revoked)
        self.assertEqual(exchange.native_exit, b"0")
        self.assertEqual(exchange.response_size, len(response))
        self.assertEqual(exchange.response_sha256, hashlib.sha256(response).hexdigest())
        receipt = json.loads(exchange.stdout)
        self.assertEqual(receipt["response_sha256"], exchange.response_sha256)
        self.assertEqual(receipt["response_size"], exchange.response_size)
        self.assertEqual(result.stdout_bytes, len(exchange.stdout))
        self.assertNotIn(exchange.stdout.decode().strip(), repr(exchange))
        self.assertNotIn(exchange.response_sha256, repr(exchange))
        self.assertNotIn("native_exit=", repr(exchange))

    def test_raw_exchange_invokes_no_receipt_parser(self) -> None:
        argv = actor_argv(sys.executable)
        with patch(
            "tests.review_bridge_pipe_fixture.json.loads",
            side_effect=AssertionError("example-parser-must-stay-outside-pump"),
        ):
            # Patch the shared json module only after argv construction. The
            # independent child imports its own module; only host parsing fails.
            exchange = _run_pipe_exchange(argv, {}, PreparedAttempt(b"example-response"))
        self.assertTrue(exchange.complete, exchange.result.error)
        self.assertTrue(exchange.result.cleanup_eof)

    def test_raw_cancellation_retains_zero_or_one_consumption(self) -> None:
        for stage, attempted in (("before_claim", False), ("after_claim", True)):
            owner = PreparedAttempt(b"example-response")
            with self.subTest(stage=stage), assert_descriptor_cleanup(self):
                exchange = _run_pipe_exchange(
                    actor_argv(sys.executable), {}, owner, cancel_at=stage
                )
            self.assertFalse(exchange.complete)
            self.assertEqual(exchange.result.attempted, attempted)
            self.assertTrue(exchange.result.revoked)
            self.assertTrue(exchange.result.cleanup_eof)
            self.assertEqual(exchange.stdout, b"")
            self.assertEqual(owner.closes, 1)
            self.assertEqual(exchange.result.error, "")

    def test_transport_completion_does_not_validate_a_forged_receipt(self) -> None:
        owner = PreparedAttempt(os.urandom(1000))
        exchange = _run_pipe_exchange(
            actor_argv(sys.executable), {}, owner, scenario="bad_receipt", timeout=3
        )
        self.assertTrue(exchange.complete, exchange.result.error)
        self.assertEqual(json.loads(exchange.stdout)["response_sha256"], "0" * 64)
        self.assertNotEqual(exchange.response_sha256, "0" * 64)
        self.assertFalse(exchange.result.success)
        legacy = run_pipe_fixture(
            actor_argv(sys.executable),
            {},
            PreparedAttempt(owner.response),
            scenario="bad_receipt",
            timeout=3,
        )
        self.assertFalse(legacy.success)
        self.assertEqual(legacy.error, "example_receipt_invalid")

    def test_native_semantic_refusal_is_fixed_and_keeps_owned_cleanup(self) -> None:
        capsule = capsule_bytes()
        owner = NativePipeAttempt(
            capsule,
            ExpectedNativeRequest("2.1.285", EXAMPLE_ENVIRONMENT, native_pipe_prompt(capsule)),
            port=12345,
            case="bearer-success",
            response=b"example-response",
        )
        with assert_descriptor_cleanup(self):
            # Old fictional actor sends capsule bytes, not a native envelope.
            exchange = _run_pipe_exchange(actor_argv(sys.executable), {}, owner, timeout=3)
        self.assertFalse(exchange.complete)
        self.assertFalse(exchange.result.attempted)
        self.assertEqual(exchange.result.error, "example_native_attempt_refused")
        self.assertTrue(exchange.result.cleanup_eof)
        self.assertTrue(owner.observation.retired)
        self.assertTrue(owner.observation.revoked)
        self.assertEqual(exchange.stdout, b"")

    def test_raw_exchange_failure_never_erases_consumption(self) -> None:
        for scenario, expected in (("wrong_digest", False), ("duplicate_request", True)):
            owner = PreparedAttempt(b"example-response")
            with self.subTest(scenario=scenario), assert_descriptor_cleanup(self):
                exchange = _run_pipe_exchange(
                    actor_argv(sys.executable), {}, owner, scenario=scenario, timeout=3
                )
            self.assertFalse(exchange.complete)
            self.assertEqual(exchange.result.attempted, expected)
            self.assertEqual(
                exchange.result.error,
                "bridge_attempt_request_invalid"
                if scenario == "wrong_digest"
                else "bridge_sequence_order_invalid",
            )
            self.assertTrue(exchange.result.cleanup_eof)
            self.assertEqual(owner.closes, 1)

    def test_legacy_deadline_limit_is_preserved_before_process_launch(self) -> None:
        with patch("tests.review_bridge_pipe_fixture.owned_fixture_process") as launch:
            for timeout in (0, -1, 10.001, 105):
                with (
                    self.subTest(timeout=timeout),
                    self.assertRaisesRegex(ValueError, "^example_fixture_budget$"),
                ):
                    run_pipe_fixture([], {}, PreparedAttempt(b"example"), timeout=timeout)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
