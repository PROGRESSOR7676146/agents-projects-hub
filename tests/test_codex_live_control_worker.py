from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from hermes_codex_router import external_worker
from hermes_codex_router.codex_appserver import RpcRejectedError, TurnResult
from hermes_codex_router.codex_live_control import CodexLiveControl, CodexLiveControlError
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import StateError
from tests import test_codex_worker as fixtures


class CallbackClient(fixtures.WorkerClient):
    on_visible_item: Any = None
    on_completed: Any = None


class CodexLiveControlWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_observer_start_failure_does_not_leave_native_callbacks_installed(self) -> None:
        job_id = self.fixture.enqueue()
        client = CallbackClient()
        worker = self.fixture.worker(client)

        class CannotStartControl(CodexLiveControl):
            def start(self) -> None:
                raise RuntimeError("Example observer thread cannot start")

        try:
            with patch.object(external_worker, "CodexLiveControl", CannotStartControl):
                self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
            self.assertEqual(client.turns, 1)
            self.assertIsNone(client.on_visible_item)
            self.assertIsNone(client.on_completed)
            checkpoint = ExecutionJournal(worker.state).read(job_id)
            assert checkpoint is not None
            self.assertEqual(checkpoint["provider_turn_id"], "turn-1")
            self.assertIsNone(checkpoint["completed_text"])
        finally:
            worker.close()

    def assert_join_timeout_recovery(self, *, after_start: bool) -> None:
        parent_id = self.fixture.enqueue()
        child_id = self.fixture.enqueue(2, "Example follow-up")
        stalled, release = threading.Event(), threading.Event()
        controls: list[CodexLiveControl] = []
        rpc_calls: list[str] = []

        class Client(CallbackClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                if not stalled.wait(2):
                    raise AssertionError("Example monitor did not reach state boundary")
                return TurnResult("Saved completed answer", 1000, 100)

            def steer_turn(self, **kwargs: Any) -> str:
                rpc_calls.append(kwargs["client_user_message_id"])
                return kwargs["turn_id"]

        class SlowControl(CodexLiveControl):
            def __init__(self, **kwargs: Any) -> None:
                original_factory = kwargs["state_factory"]

                def factory():
                    state = original_factory()
                    method = "start_steer_followup" if after_start else "lease_steer_followup"
                    original = getattr(state, method)

                    def pause(*args: Any, **options: Any):
                        result = original(*args, **options)
                        if result is not None:
                            stalled.set()
                            release.wait(5)
                        return result

                    setattr(state, method, pause)
                    return state

                kwargs["state_factory"] = factory
                kwargs["poll_seconds"] = 0.01
                super().__init__(**kwargs)
                controls.append(self)

            def stop_and_join(self, *, timeout: float = 10) -> None:
                super().stop_and_join(timeout=0.01)

        client = Client()
        worker = self.fixture.worker(client)
        try:
            with patch.object(external_worker, "CodexLiveControl", SlowControl):
                self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(parent_id).status, "result_ready")
            self.assertEqual(
                worker.state.get_provider_job(child_id).status,
                "executing" if after_start else "leased",
            )
            self.assertEqual(client.turns, 1)
            self.assertEqual(rpc_calls, [])
            # Recover the expired child while the old observer is still stalled.
            worker.state.recover_stale_provider_jobs(
                now=datetime.now(timezone.utc) + timedelta(minutes=3)
            )
            self.assertEqual(
                worker.state.get_provider_job(child_id).status,
                "indeterminate" if after_start else "queued",
            )
            if after_start:
                with self.assertRaises(StateError):
                    self.fixture.enqueue(3, "Example new root work")
            release.set()
            CodexLiveControl.stop_and_join(controls[0])
            self.assertEqual(rpc_calls, [])
            self.assertIsNone(client.on_visible_item)
            self.assertIsNone(client.on_completed)
        finally:
            release.set()
            for control in controls:
                CodexLiveControl.stop_and_join(control)
            worker.close()
        resumed = self.fixture.worker(client)
        try:
            # Parent final delivery still fences this topic; executed child is never replayed.
            self.assertFalse(resumed.run_cycle())
            self.assertEqual(client.turns, 1)
            self.assertEqual(rpc_calls, [])
        finally:
            resumed.close()

    def test_saved_parent_recovery_after_join_timeout_before_child_start_is_safe(self) -> None:
        self.assert_join_timeout_recovery(after_start=False)

    def test_saved_parent_recovery_after_join_timeout_keeps_executing_child_blocker(self) -> None:
        self.assert_join_timeout_recovery(after_start=True)

    def test_rejected_steer_runs_once_as_normal_child_after_parent_delivery(self) -> None:
        parent_id = self.fixture.enqueue()
        child_id = self.fixture.enqueue(2, "Example follow-up")
        rejected, later_polls = threading.Event(), threading.Event()
        calls: list[str] = []

        class Client(CallbackClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                if not later_polls.wait(2):
                    raise AssertionError("Example stop polls did not continue after rejection")
                return TurnResult("Example completed answer", 1000, 100)

            def steer_turn(self, **kwargs: Any) -> str:
                calls.append(kwargs["client_user_message_id"])
                rejected.set()
                raise RpcRejectedError("Example unsupported steering")

        class FastControl(CodexLiveControl):
            def __init__(self, **kwargs: Any) -> None:
                original_factory = kwargs["state_factory"]

                def factory():
                    state = original_factory()
                    lookup = state.pending_emergency_stop_for_job
                    polls = 0

                    def observe(*args: Any, **options: Any):
                        nonlocal polls
                        if rejected.is_set():
                            polls += 1
                            if polls >= 3:
                                later_polls.set()
                        return lookup(*args, **options)

                    state.pending_emergency_stop_for_job = observe
                    return state

                kwargs["state_factory"] = factory
                kwargs["poll_seconds"] = 0.01
                super().__init__(**kwargs)

        client = Client()
        worker = self.fixture.worker(client)
        try:
            with patch.object(external_worker, "CodexLiveControl", FastControl):
                self.assertTrue(worker.run_cycle())
            self.assertEqual(calls, [child_id])
            self.assertEqual(worker.state.get_provider_job(parent_id).status, "result_ready")
            self.assertEqual(worker.state.get_provider_job(child_id).status, "queued")
            self.assertFalse(worker.run_cycle())
            outbox = worker.state.lease_telegram_outbox("codex", "example-sender")
            assert outbox is not None and outbox.lease_token is not None
            worker.state.mark_telegram_outbox_delivered(
                outbox.outbox_id, outbox.lease_token, telegram_message_id=901
            )
            self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(child_id).status, "result_ready")
            self.assertEqual(client.turns, 2)
            self.assertEqual(calls, [child_id])
        finally:
            worker.close()

    def assert_primary_failure(self, *, callback_failure: bool) -> None:
        parent_id = self.fixture.enqueue()
        primary = ValueError("Example native wait or callback failure")

        class Client(CallbackClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                self.on_visible_item("example-visible", "Saved incomplete answer", "commentary")
                raise primary

        class FailedCleanupControl(CodexLiveControl):
            def start(self) -> None:
                self._deferred_failure = OSError("Example independent steering failure")

            def stop_and_join(self, *, timeout: float = 10) -> None:
                raise CodexLiveControlError("Example independent shutdown failure")

        client = Client()
        worker = self.fixture.worker(client)
        item_write = (
            patch.object(ExecutionJournal, "record_item", side_effect=primary)
            if callback_failure
            else patch.object(
                ExecutionJournal,
                "record_item",
                autospec=True,
                side_effect=ExecutionJournal.record_item,
            )
        )
        try:
            with (
                patch.object(external_worker, "CodexLiveControl", FailedCleanupControl),
                patch("hermes_codex_router.codex_live_control.survived") as diagnostic,
                item_write,
            ):
                self.assertTrue(worker.run_cycle())
            failed = worker.state.get_provider_job(parent_id)
            self.assertEqual((failed.status, failed.error_code), ("indeterminate", "ValueError"))
            self.assertEqual(failed.error_detail, str(primary))
            self.assertEqual(diagnostic.call_args.args[0], "codex_live_control.shutdown")
            notice = worker.state.get_telegram_outbox_for_job(parent_id).telegram_html
            if not callback_failure:
                self.assertIn("Saved incomplete answer", notice)
            self.assertIsNone(client.on_visible_item)
            self.assertIsNone(client.on_completed)
            worker.run_cycle()  # A read-only uncertainty observation may count as work.
            self.assertEqual(worker.state.get_provider_job(parent_id).status, "indeterminate")
            self.assertEqual(client.turns, 1)
        finally:
            worker.close()

    def test_native_wait_failure_remains_primary_when_shutdown_also_fails(self) -> None:
        self.assert_primary_failure(callback_failure=False)

    def test_visible_callback_failure_remains_primary_when_shutdown_also_fails(self) -> None:
        self.assert_primary_failure(callback_failure=True)

    def assert_stop_precedes_deferred_fault(self, *, confirmed: bool) -> None:
        parent_id = self.fixture.enqueue()
        client = CallbackClient()
        worker = self.fixture.worker(client)
        deferred_calls = []

        class FailedSteeringControl(CodexLiveControl):
            def start(self) -> None:
                self._deferred_failure = ValueError("Example permanent steering failure")
                if confirmed:
                    self.confirmed_interrupt_request = "example-stop"

            def stop_and_join(self, *, timeout: float = 10) -> None:
                pass

            def raise_deferred_failure(self) -> None:
                deferred_calls.append(1)
                super().raise_deferred_failure()

        try:
            leased = worker.state.lease_provider_job("codex", worker.worker_id)
            assert leased is not None and leased.lease_token is not None
            job = worker.state.mark_provider_job_executing(parent_id, leased.lease_token)
            with (
                patch.object(external_worker, "CodexLiveControl", FailedSteeringControl),
                patch.object(
                    worker.state,
                    "pending_emergency_stop_for_job",
                    side_effect=OSError("Example late stop lookup failed") if confirmed else None,
                    return_value="example-stop",
                ) as lookup,
                self.assertRaises(external_worker.ProviderTurnStopped),
            ):
                worker._execute_codex(
                    job,
                    leased.lease_token,
                    self.fixture.registry.projects[0],
                    worker.state.get_topic(job.topic_id),
                )
            self.assertEqual(deferred_calls, [])
            self.assertEqual(lookup.call_count, 0 if confirmed else 1)
            self.assertEqual(client.turns, 1)
        finally:
            worker.close()

    def test_confirmed_stop_precedes_deferred_fault_and_failing_late_lookup(self) -> None:
        self.assert_stop_precedes_deferred_fault(confirmed=True)

    def test_late_stop_precedes_deferred_steering_fault(self) -> None:
        self.assert_stop_precedes_deferred_fault(confirmed=False)


if __name__ == "__main__":
    unittest.main()
