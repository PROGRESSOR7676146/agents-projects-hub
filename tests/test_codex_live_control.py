from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from hermes_codex_router.codex_appserver import RpcRejectedError, StoredTurnOutcome
from hermes_codex_router.codex_live_control import CodexLiveControl, CodexLiveControlError
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.sqlite_contention import is_sqlite_contention
from hermes_codex_router.state import HubState
from tests import test_codex_worker as fixtures


def busy(code: int = sqlite3.SQLITE_BUSY) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("Example contention")
    error.sqlite_errorcode = code
    return error


class ControlClient:
    def __init__(self) -> None:
        self.steers: list[dict[str, Any]] = []
        self.interrupts: list[dict[str, Any]] = []
        self.steered = threading.Event()
        self.interrupted = threading.Event()
        self.error: Exception | None = None

    def steer_turn(self, **kwargs: Any) -> str:
        self.steers.append(kwargs)
        self.steered.set()
        if self.error is not None:
            raise self.error
        return kwargs["turn_id"]

    def read_turn_outcome(self, **kwargs: Any) -> StoredTurnOutcome:
        return StoredTurnOutcome("active")

    def interrupt_turn(self, **kwargs: Any) -> None:
        self.interrupts.append(kwargs)
        self.interrupted.set()

    def close(self) -> None:
        pass


class SqliteContentionClassificationTests(unittest.TestCase):
    def test_only_primary_or_extended_busy_locked_operational_errors_are_retryable(self) -> None:
        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, 261, 517, 262):
            with self.subTest(code=code):
                self.assertTrue(is_sqlite_contention(busy(code)))
        for error in (
            sqlite3.OperationalError("database is locked"),
            busy(sqlite3.SQLITE_IOERR),
            busy(sqlite3.SQLITE_ERROR),
            sqlite3.IntegrityError("database is locked"),
            RuntimeError("database is locked"),
        ):
            with self.subTest(error=error):
                self.assertFalse(is_sqlite_contention(error))


class CodexLiveControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        parent_id = self.fixture.enqueue()
        self.state = HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.job = self.state.mark_provider_job_executing(parent_id, leased.lease_token)
        self.journal = ExecutionJournal(self.state)
        self.journal.record_thread(
            parent_id,
            leased.lease_token,
            "thread-1",
            self.fixture.registry.projects[0].root,
            codex_permission_profile=None,
        )
        self.journal.record_turn(parent_id, leased.lease_token, "turn-1")
        self.client = ControlClient()
        self.opened = threading.Event()
        self.controls: list[CodexLiveControl] = []
        self.addCleanup(self.close_controls)

    def close_controls(self) -> None:
        for control in self.controls:
            try:
                control.stop_and_join()
            except CodexLiveControlError:
                pass

    def stop_request(self) -> str:
        request, _, _ = self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        return request

    def test_socket_stop_without_send_keeps_primary_stream_open(self) -> None:
        request = self.stop_request()
        for failure in ("acquire", "read", "unknown", "guard"):
            with self.subTest(failure=failure):
                closed = []

                def acquire():
                    if failure == "acquire":
                        raise OSError("Example temporary connection failure")
                    return self.client

                control = CodexLiveControl(
                    config=self.fixture.config,
                    state_factory=lambda: self.state,
                    client_factory=acquire,
                    job=self.job,
                    worker_id="example-worker",
                    thread_id="thread-1",
                    turn_id="turn-1",
                    transport_mode="socket",
                    close_owned_turn_client=lambda: closed.append(True),
                )
                with (
                    patch.object(
                        self.client,
                        "read_turn_outcome",
                        side_effect=OSError("Example unavailable observation")
                        if failure == "read"
                        else None,
                        return_value=StoredTurnOutcome(
                            "unknown" if failure == "unknown" else "active"
                        ),
                    ),
                    patch(
                        "hermes_codex_router.codex_live_control.begin_codex_interrupt",
                        return_value=None,
                    ),
                ):
                    control._interrupt(self.state, request)
                self.assertEqual(closed, [])
                self.assertEqual(self.client.interrupts, [])
                target = self.state.codex_controls.read(self.job.job_id)
                assert target is not None
                self.assertIsNone(target["send_started_at"])

    def test_post_send_completion_contention_still_wakes_primary_without_second_rpc(self) -> None:
        request = self.stop_request()
        closed = []
        control = CodexLiveControl(
            config=self.fixture.config,
            state_factory=lambda: self.state,
            client_factory=lambda: self.client,
            job=self.job,
            worker_id="example-worker",
            thread_id="thread-1",
            turn_id="turn-1",
            transport_mode="socket",
            close_owned_turn_client=lambda: closed.append(True),
        )
        from hermes_codex_router.codex_appserver import TurnResult

        with (
            patch.object(
                self.client,
                "read_turn_outcome",
                side_effect=[
                    StoredTurnOutcome("active"),
                    StoredTurnOutcome("completed", TurnResult("Example saved final", None, None)),
                ],
            ),
            patch.object(ExecutionJournal, "record_completion", side_effect=busy()),
            self.assertRaises(sqlite3.OperationalError),
        ):
            control._interrupt(self.state, request)
        self.assertEqual(len(self.client.interrupts), 1)
        self.assertEqual(closed, [True])
        control._interrupt(self.state, request)
        self.assertEqual(len(self.client.interrupts), 1)

    def control(self, *, decorate=lambda state: None, before_open=lambda: None, **changes):
        def state_factory():
            before_open()
            state = HubState.open_existing(
                self.fixture.config.state_path,
                codex_permission_profile=None,
                contention_timeout_seconds=0.01,
            )
            decorate(state)
            self.opened.set()
            return state

        options: dict[str, Any] = dict(
            config=self.fixture.config,
            state_factory=state_factory,
            client_factory=lambda: self.client,
            job=self.job,
            worker_id="example-worker",
            thread_id="thread-1",
            turn_id="turn-1",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
            poll_seconds=0.01,
        )
        options.update(changes)
        control = CodexLiveControl(**options)
        self.controls.append(control)
        control.start()
        return control

    def test_open_and_stop_lookup_contention_recover_and_interrupt_exactly_once(self) -> None:
        attempts = 0
        lookup_attempts = 0

        def before_open():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise busy()

        def decorate(state):
            original = state.pending_emergency_stop_for_job

            def lookup(*args, **kwargs):
                nonlocal lookup_attempts
                lookup_attempts += 1
                if lookup_attempts == 1:
                    raise busy(sqlite3.SQLITE_LOCKED)
                return original(*args, **kwargs)

            state.pending_emergency_stop_for_job = lookup

        request = self.stop_request()
        control = self.control(decorate=decorate, before_open=before_open)
        self.assertTrue(self.client.interrupted.wait(2))
        control.stop_and_join()
        self.assertEqual((attempts, lookup_attempts), (2, 2))
        self.assertEqual(len(self.client.interrupts), 1)
        self.assertEqual(
            {key: self.client.interrupts[0][key] for key in ("thread_id", "turn_id")},
            {"thread_id": "thread-1", "turn_id": "turn-1"},
        )
        self.assertEqual(control.confirmed_interrupt_request, request)
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "executing")

    def test_lease_contention_recovers_and_followup_is_steered_at_most_once(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        attempts = 0

        def decorate(state):
            original = state.lease_steer_followup

            def lease(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise busy()
                return original(*args, **kwargs)

            state.lease_steer_followup = lease

        control = self.control(decorate=decorate)
        self.assertTrue(self.client.steered.wait(2))
        self.stop_request()
        self.assertTrue(self.client.interrupted.wait(2))
        control.stop_and_join()
        self.assertEqual(len(self.client.steers), 1)
        self.assertEqual(self.client.steers[0]["client_user_message_id"], child)
        self.assertEqual(self.state.get_provider_job(child).status, "completed")

    def test_start_busy_after_committed_lease_does_not_steer_and_stop_still_works(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        failed = threading.Event()

        def decorate(state):
            def start(*args, **kwargs):
                failed.set()
                raise busy()

            state.start_steer_followup = start

        control = self.control(decorate=decorate)
        self.assertTrue(failed.wait(2))
        self.assertEqual(self.state.get_provider_job(child).status, "leased")
        self.stop_request()
        self.assertTrue(self.client.interrupted.wait(2))
        control.stop_and_join()
        self.assertEqual(self.client.steers, [])
        retained = self.state.get_provider_job(child)
        self.assertEqual(retained.status, "queued")
        self.assertIsNone(retained.lease_token)
        self.assertEqual(retained.attempt_count, 0)

    def test_accepted_steer_busy_settlement_keeps_execution_nonreplayable(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        settlement_failed = threading.Event()

        def decorate(state):
            def fail_complete(*args, **kwargs):
                settlement_failed.set()
                raise busy()

            def fail_uncertainty(*args, **kwargs):
                raise AssertionError("Transient completion contention must retain its write")

            state.complete_steered_job = fail_complete
            state.mark_provider_job_indeterminate = fail_uncertainty

        control = self.control(decorate=decorate)
        self.assertTrue(settlement_failed.wait(2))
        self.assertEqual(self.state.get_provider_job(child).status, "executing")
        self.stop_request()
        self.assertTrue(self.client.interrupted.wait(2))
        control.stop_and_join()
        self.assertEqual(len(self.client.steers), 1)
        self.assertNotEqual(self.state.get_provider_job(child).status, "queued")
        self.state.recover_stale_provider_jobs(
            now=datetime.now(timezone.utc) + timedelta(minutes=3)
        )
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")
        self.assertIsNone(self.state.lease_provider_job("codex", "example-restarted-worker"))

    def test_unknown_steer_busy_marking_retains_nonreplayable_execution(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        self.client.error = EOFError("Example unknown provider outcome")
        failed = threading.Event()

        def decorate(state):
            def fail_unknown(*args, **kwargs):
                failed.set()
                raise busy()

            state.mark_provider_job_indeterminate = fail_unknown

        control = self.control(decorate=decorate)
        self.assertTrue(failed.wait(2))
        self.stop_request()
        self.assertTrue(self.client.interrupted.wait(2))
        control.stop_and_join()
        self.assertEqual(len(self.client.steers), 1)
        self.assertEqual(self.state.get_provider_job(child).status, "executing")
        self.state.recover_stale_provider_jobs(
            now=datetime.now(timezone.utc) + timedelta(minutes=3)
        )
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")
        self.assertIsNone(self.state.lease_provider_job("codex", "example-restarted-worker"))

    def test_provider_contention_error_does_not_repeat_interrupt_or_confirm_it(self) -> None:
        self.stop_request()
        attempted = threading.Event()

        def fail_interrupt(**kwargs):
            self.client.interrupts.append(kwargs)
            attempted.set()
            raise busy()

        self.client.interrupt_turn = fail_interrupt
        control = self.control()
        self.assertTrue(attempted.wait(2))
        control.stop_and_join()
        self.assertEqual(len(self.client.interrupts), 1)
        self.assertIsNone(control.confirmed_interrupt_request)

    def test_stdio_close_failure_is_unconfirmed_and_attempted_once(self) -> None:
        self.stop_request()
        attempted = threading.Event()
        calls = []

        def fail_close():
            calls.append(1)
            attempted.set()
            raise OSError("Example owned transport close failure")

        control = self.control(transport_mode="stdio-fallback", close_owned_turn_client=fail_close)
        self.assertTrue(attempted.wait(2))
        control.stop_and_join()
        self.assertEqual(calls, [1])
        self.assertIsNone(control.confirmed_interrupt_request)

    def test_shutdown_during_client_factory_closes_returned_client_without_rpc(self) -> None:
        self.stop_request()
        acquired, release, closed = threading.Event(), threading.Event(), threading.Event()

        def factory():
            acquired.set()
            release.wait(2)
            return self.client

        self.client.close = closed.set
        control = self.control(client_factory=factory)
        self.assertTrue(acquired.wait(2))
        with self.assertRaises(CodexLiveControlError):
            control.stop_and_join(timeout=0.01)
        release.set()
        control.stop_and_join()
        self.assertTrue(closed.is_set())
        self.assertEqual(self.client.interrupts, [])

    def test_shutdown_cancels_active_steer_and_retains_uncertainty_without_repeat(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        active, closed = threading.Event(), threading.Event()

        def steer(**kwargs):
            self.client.steers.append(kwargs)
            active.set()
            closed.wait(2)
            raise EOFError("Example cancelled active control transport")

        self.client.steer_turn = steer
        self.client.close = closed.set
        control = self.control()
        self.assertTrue(active.wait(2))
        control.stop_and_join()
        self.assertTrue(closed.is_set())
        self.assertEqual(len(self.client.steers), 1)
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")

    def test_shutdown_during_state_open_closes_connection_without_polling(self) -> None:
        acquired, release = threading.Event(), threading.Event()
        closed = threading.Event()

        def decorate(state):
            original_close = state.close

            def close():
                original_close()
                closed.set()

            state.close = close
            acquired.set()
            release.wait(2)

        control = self.control(decorate=decorate)
        self.assertTrue(acquired.wait(2))
        with self.assertRaises(CodexLiveControlError):
            control.stop_and_join(timeout=0.01)
        release.set()
        control.stop_and_join()
        self.assertTrue(closed.is_set())
        self.assertEqual(self.client.steers, [])
        self.assertEqual(self.client.interrupts, [])

    def test_rejected_steer_busy_requeue_is_not_resent_and_stop_still_works(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        self.client.error = RpcRejectedError("Example rejection")
        rejected = threading.Event()

        def decorate(state):
            def fail_requeue(*args, **kwargs):
                rejected.set()
                raise busy()

            state.reject_unaccepted_steer = fail_requeue

        control = self.control(decorate=decorate)
        self.assertTrue(rejected.wait(2))
        self.stop_request()
        self.assertTrue(self.client.interrupted.wait(2))
        control.stop_and_join()
        self.assertEqual(len(self.client.steers), 1)
        self.assertNotEqual(self.state.get_provider_job(child).status, "queued")

    def test_shutdown_during_lease_starts_no_provider_call_and_closes_state(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        leased = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        def decorate(state):
            original_close = state.close

            def close():
                original_close()
                closed.set()

            state.close = close
            original = state.lease_steer_followup

            def lease(*args, **kwargs):
                result = original(*args, **kwargs)
                if result is not None:
                    leased.set()
                    release.wait(2)
                return result

            state.lease_steer_followup = lease

        control = self.control(decorate=decorate)
        self.assertTrue(leased.wait(2))
        with self.assertRaises(CodexLiveControlError):
            control.stop_and_join(timeout=0.01)
        release.set()
        control.stop_and_join()
        self.assertTrue(closed.is_set())
        self.assertEqual(self.client.steers, [])
        self.assertEqual(self.state.get_provider_job(child).status, "queued")

    def test_noncontention_sqlite_failure_is_visible_and_not_retried(self) -> None:
        failed = threading.Event()
        attempts = 0

        def before_open():
            nonlocal attempts
            attempts += 1
            failed.set()
            raise busy(sqlite3.SQLITE_IOERR)

        control = self.control(before_open=before_open)
        self.assertTrue(failed.wait(2))
        with self.assertRaises(CodexLiveControlError):
            control.stop_and_join()
        self.assertEqual(attempts, 1)

    def test_settlement_preserves_noncontention_error_even_if_unknown_marking_fails(self) -> None:
        for marking_code in (None, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR):
            with self.subTest(marking_code=marking_code):
                child_id = self.fixture.enqueue(10 + len(self.controls), "Example follow-up")
                child = self.state.lease_steer_followup(self.job.job_id, "example-steer")
                assert child is not None and child.lease_token is not None
                original = busy(sqlite3.SQLITE_ERROR)
                control = CodexLiveControl(
                    config=self.fixture.config,
                    state_factory=lambda: self.state,
                    client_factory=lambda: self.client,
                    job=self.job,
                    worker_id="example-worker",
                    thread_id="thread-1",
                    turn_id="turn-1",
                    transport_mode="socket",
                    close_owned_turn_client=lambda: None,
                )
                self.controls.append(control)
                marking = (
                    patch.object(
                        self.state,
                        "mark_provider_job_indeterminate",
                        side_effect=busy(marking_code),
                    )
                    if marking_code is not None
                    else patch.object(
                        self.state,
                        "mark_provider_job_indeterminate",
                        wraps=self.state.mark_provider_job_indeterminate,
                    )
                )
                with (
                    patch.object(self.state, "lease_steer_followup", return_value=child),
                    patch.object(self.state, "complete_steered_job", side_effect=original),
                    self.assertRaises(sqlite3.OperationalError) as raised,
                ):
                    control._steer(self.state)
                self.assertIs(raised.exception, original)
                with marking:
                    if marking_code is None:
                        control._steer(self.state)
                    else:
                        with self.assertRaises(sqlite3.OperationalError):
                            control._steer(self.state)
                with self.assertRaises(CodexLiveControlError) as deferred:
                    control.raise_deferred_failure()
                self.assertIs(deferred.exception.__cause__, original)
                self.assertEqual(
                    self.state.get_provider_job(child_id).status,
                    "indeterminate" if marking_code is None else "executing",
                )
                self.assertEqual(len(self.client.steers), len(self.controls))
                if marking_code is not None:
                    self.state.mark_provider_job_indeterminate(
                        child_id,
                        child.lease_token,
                        error_code="example",
                        error_detail="Example unknown outcome",
                    )
                self.state.resolve_indeterminate_job(child_id, "acknowledged")

    def test_running_preserves_primary_error_and_reports_independent_shutdown_failure(self) -> None:
        control = CodexLiveControl(
            config=self.fixture.config,
            state_factory=lambda: self.state,
            client_factory=lambda: self.client,
            job=self.job,
            worker_id="example-worker",
            thread_id="thread-1",
            turn_id="turn-1",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
        )
        primary = ValueError("Example native callback failure")
        with (
            patch.object(control, "start") as start,
            patch.object(
                control,
                "stop_and_join",
                side_effect=CodexLiveControlError("Example incomplete shutdown"),
            ),
            patch("hermes_codex_router.codex_live_control.survived") as report,
            self.assertRaises(ValueError) as raised,
        ):
            with control.running():
                raise primary
        self.assertIs(raised.exception, primary)
        start.assert_called_once()
        self.assertEqual(report.call_args.args[0], "codex_live_control.shutdown")

    def test_explicit_rejection_leaves_fifo_child_queued_without_repeating_steer(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        self.client.error = RpcRejectedError("Example unsupported steering")
        control = CodexLiveControl(
            config=self.fixture.config,
            state_factory=lambda: self.state,
            client_factory=lambda: self.client,
            job=self.job,
            worker_id="example-worker",
            thread_id="thread-1",
            turn_id="turn-1",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
        )
        control._steer(self.state)
        control._steer(self.state)
        self.assertEqual(len(self.client.steers), 1)
        self.assertEqual(self.state.get_provider_job(child).status, "queued")

    def test_rejection_latch_survives_busy_settlement_and_keeps_stop_available(self) -> None:
        child = self.fixture.enqueue(2, "Example follow-up")
        self.client.error = RpcRejectedError("Example unsupported steering")
        control = CodexLiveControl(
            config=self.fixture.config,
            state_factory=lambda: self.state,
            client_factory=lambda: self.client,
            job=self.job,
            worker_id="example-worker",
            thread_id="thread-1",
            turn_id="turn-1",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
        )
        with (
            patch.object(self.state, "reject_unaccepted_steer", side_effect=busy()),
            self.assertRaises(sqlite3.OperationalError),
        ):
            control._steer(self.state)
        control._steer(self.state)
        control._steer(self.state)
        self.assertEqual(len(self.client.steers), 1)
        self.assertEqual(self.state.get_provider_job(child).status, "queued")
        request = self.stop_request()
        control._interrupt(self.state, request)
        self.assertEqual(len(self.client.interrupts), 1)
        self.assertEqual(control.confirmed_interrupt_request, request)

    def test_busy_absorption_does_not_attempt_uncertainty_marking(self) -> None:
        self.fixture.enqueue(2, "Example follow-up")
        control = CodexLiveControl(
            config=self.fixture.config,
            state_factory=lambda: self.state,
            client_factory=lambda: self.client,
            job=self.job,
            worker_id="example-worker",
            thread_id="thread-1",
            turn_id="turn-1",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
        )
        original, marking = busy(), busy(sqlite3.SQLITE_IOERR)
        with (
            patch.object(self.state, "complete_steered_job", side_effect=original),
            patch.object(
                self.state, "mark_provider_job_indeterminate", side_effect=marking
            ) as mark,
            self.assertRaises(sqlite3.OperationalError) as raised,
        ):
            control._steer(self.state)
        self.assertIs(raised.exception, original)
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
