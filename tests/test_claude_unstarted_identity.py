from __future__ import annotations

import unittest
from typing import Any

from hermes_codex_router.claude_stream import ClaudeTerminalFailure, ClaudeVisibleAssistant
from hermes_codex_router.external_runtime import ExternalTurnResult, ProviderUnavailableError
from hermes_codex_router.worker_execution import classify_worker_failure
from tests import test_claude_native_worker as fixtures


class SequenceClaudeAdapter(fixtures.ObservingClaudeAdapter):
    def __init__(self, state_path, failures: tuple[str, ...]) -> None:
        super().__init__(state_path)
        self.failures = failures

    def run_turn(self, **kwargs: Any) -> ExternalTurnResult:
        index = len(self.calls)
        if index < len(self.failures):
            code = self.failures[index]
            if code:
                self.calls.append(kwargs)
                native = kwargs["session_id"] or kwargs["new_session_id"]
                if code == "terminal":
                    raise ClaudeTerminalFailure(
                        "claude_quota_exhausted", "Fictional terminal rejection.", native
                    )
                if code == "partial":
                    kwargs["on_visible_assistant"](
                        ClaudeVisibleAssistant(
                            native, fixtures.OTHER_UUID, "Fictional visible text."
                        )
                    )
                    raise ProviderUnavailableError(
                        "claude_cli_unavailable", "Fictional contradictory launch failure."
                    )
                raise ProviderUnavailableError(code, "Fictional pre-invocation failure.")
        return super().run_turn(**kwargs)


class ClaudeUnstartedIdentityTests(unittest.TestCase):
    def seed(self, failures: tuple[str, ...]):
        fixture = fixtures.ClaudeNativeWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = SequenceClaudeAdapter(fixture.path, failures)
        return fixture, adapter, fixture.worker(adapter)

    def test_exact_preinvocation_codes_use_same_allocated_uuid_for_second_start(self) -> None:
        for code in (
            "claude_cpa_route_unverified",
            "claude_cpa_credential_ambiguous",
            "claude_cli_unavailable",
        ):
            with self.subTest(code=code):
                f, a, w = self.seed((code,))
                first = f.enqueue(1)
                f.enqueue(2)
                w.run_cycle()
                self.assertEqual(w.state.get_provider_job(first).error_class, "pre_execution")
                w.run_cycle()
                self.assertEqual(len(a.calls), 2)
                self.assertIsNone(a.calls[1]["session_id"])
                self.assertEqual(a.calls[1]["new_session_id"], a.calls[0]["new_session_id"])

    def test_multiple_proven_unstarted_attempts_keep_same_start_identity(self) -> None:
        f, a, w = self.seed(("claude_cpa_route_unverified", "claude_cli_unavailable"))
        jobs = [f.enqueue(i) for i in range(1, 4)]
        for _ in jobs:
            w.run_cycle()
        self.assertEqual(len(a.calls), 3)
        self.assertTrue(all(call["session_id"] is None for call in a.calls))
        self.assertEqual(len({call["new_session_id"] for call in a.calls}), 1)

    def test_missing_older_checkpoint_cannot_recreate_native_session(self) -> None:
        for missing_snapshot in (False, True):
            for outcome in ("terminal", "claude_provider_failure", "claude_cli_unavailable"):
                with self.subTest(missing_snapshot=missing_snapshot, outcome=outcome):
                    f, a, w = self.seed((outcome, "claude_cli_unavailable"))
                    first = f.enqueue(1)
                    f.enqueue(2)
                    f.enqueue(3)
                    w.run_cycle()
                    w.run_cycle()
                    native = a.calls[0]["new_session_id"]
                    with w.state._connection:
                        w.state._connection.execute(
                            "DELETE FROM provider_execution_checkpoints WHERE job_id=?", (first,)
                        )
                        w.state._connection.execute(
                            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
                            (None if missing_snapshot else native, first),
                        )
                    w.run_cycle()
                    self.assertEqual(len(a.calls), 3)
                    self.assertEqual(a.calls[2]["session_id"], native)
                    self.assertIsNone(a.calls[2]["new_session_id"])

    def test_native_terminal_failure_requires_resume(self) -> None:
        f, a, w = self.seed(("terminal",))
        f.enqueue(1)
        f.enqueue(2)
        w.run_cycle()
        w.run_cycle()
        self.assertEqual(a.calls[1]["session_id"], a.calls[0]["new_session_id"])
        self.assertIsNone(a.calls[1]["new_session_id"])

    def test_visible_evidence_disqualifies_even_claimed_preinvocation_failure(self) -> None:
        f, a, w = self.seed(("partial",))
        f.enqueue(1)
        f.enqueue(2)
        w.run_cycle()
        w.run_cycle()
        self.assertEqual(a.calls[1]["session_id"], a.calls[0]["new_session_id"])
        self.assertIsNone(a.calls[1]["new_session_id"])

    def test_generic_provider_unavailability_is_not_noninvocation_proof(self) -> None:
        f, a, w = self.seed(("claude_provider_failure",))
        first = f.enqueue(1)
        f.enqueue(2)
        w.run_cycle()
        self.assertEqual(w.state.get_provider_job(first).error_class, "provider_unavailable")
        w.run_cycle()
        self.assertEqual(a.calls[1]["session_id"], a.calls[0]["new_session_id"])
        self.assertIsNone(a.calls[1]["new_session_id"])

    def test_preinvocation_classification_requires_claude_runtime_and_exact_code(self) -> None:
        error = ProviderUnavailableError("claude_cli_unavailable", "Fictional launch failure.")
        self.assertEqual(
            classify_worker_failure(error, runtime="claude").error_class, "pre_execution"
        )
        self.assertEqual(
            classify_worker_failure(error, runtime="opencode").error_class, "provider_unavailable"
        )
        error = ProviderUnavailableError("claude_provider_failure", "Fictional provider failure.")
        self.assertEqual(
            classify_worker_failure(error, runtime="claude").error_class, "provider_unavailable"
        )
