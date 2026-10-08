from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from hermes_codex_router.codex_appserver import RpcRejectedError
from hermes_codex_router.codex_live_control import CodexLiveControl, CodexLiveControlError
from hermes_codex_router.state import StateError
from tests import test_codex_live_control as fixtures


class CodexControlSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexLiveControlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.client = self.fixture.client

    def direct_control(self, **changes):
        options: dict[str, Any] = dict(
            config=self.fixture.fixture.config,
            state_factory=lambda: self.state,
            client_factory=lambda: self.client,
            job=self.fixture.job,
            worker_id="example-worker",
            thread_id="thread-1",
            turn_id="turn-1",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
        )
        options.update(changes)
        return CodexLiveControl(**options)

    def test_shutdown_waits_for_fenced_interrupt_reply_and_settlement(self) -> None:
        self.fixture.stop_request()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def interrupt(**kwargs):
            self.client.interrupts.append(kwargs)
            entered.set()
            release.wait(2)
            if closed.is_set():
                raise EOFError("Example shutdown destroyed matched reply")

        self.client.interrupt_turn = interrupt
        self.client.close = closed.set
        control = self.fixture.control()
        self.assertTrue(entered.wait(2))

        def shutdown():
            try:
                control.stop_and_join()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=shutdown)
        thread.start()
        try:
            self.assertFalse(closed.wait(0.05))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        row = self.state.codex_controls.read(self.fixture.job.job_id)
        assert row is not None
        self.assertEqual(row["interrupt_outcome"], "matched_ack")
        self.assertIsNotNone(row["owner_quiesced_at"])
        self.assertEqual(len(self.client.interrupts), 1)

    def test_matched_ack_survives_real_sqlite_writer_contention(self) -> None:
        self.fixture.stop_request()
        entered, release, settled = threading.Event(), threading.Event(), threading.Event()
        attempts = []

        def decorate(state):
            finish = state.codex_controls.finish_interrupt

            def settle(*args, **kwargs):
                attempts.append(1)
                finish(*args, **kwargs)
                settled.set()

            state.codex_controls.finish_interrupt = settle

        def interrupt(**kwargs):
            self.client.interrupts.append(kwargs)
            entered.set()
            release.wait(2)

        self.client.interrupt_turn = interrupt
        control = self.fixture.control(decorate=decorate)
        self.assertTrue(entered.wait(2))
        blocker = sqlite3.connect(self.fixture.fixture.config.state_path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            release.set()
            self.assertFalse(settled.wait(0.15))
            blocker.execute("ROLLBACK")
            self.assertTrue(settled.wait(2))
        finally:
            blocker.close()
            release.set()
            control.stop_and_join()
        row = self.state.codex_controls.read(self.fixture.job.job_id)
        assert row is not None
        self.assertEqual(row["interrupt_outcome"], "matched_ack")
        self.assertIsNotNone(row["owner_quiesced_at"])
        self.assertGreater(len(attempts), 1)
        self.assertEqual(len(self.client.interrupts), 1)

    def test_permanent_absorption_failure_keeps_stop_polling_and_defers_original_error(
        self,
    ) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        failed = threading.Event()
        original = fixtures.busy(sqlite3.SQLITE_IOERR)

        def decorate(state):
            def fail(*args, **kwargs):
                failed.set()
                raise original

            state.complete_steered_job = fail

        control = self.fixture.control(decorate=decorate)
        self.assertTrue(failed.wait(2))
        self.fixture.stop_request()
        self.assertTrue(self.client.interrupted.wait(1))
        control.stop_and_join()
        with self.assertRaises(CodexLiveControlError) as raised:
            control.raise_deferred_failure()
        self.assertIs(raised.exception.__cause__, original)
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")
        self.assertEqual(len(self.client.steers), 1)

    def assert_transient_settlement(self, *, rejection: bool) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        if rejection:
            self.client.error = RpcRejectedError("Example rejection")
        settled = threading.Event()
        attempts = 0

        def decorate(state):
            name = "reject_unaccepted_steer" if rejection else "complete_steered_job"
            original = getattr(state, name)

            def write(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise fixtures.busy()
                result = original(*args, **kwargs)
                settled.set()
                return result

            setattr(state, name, write)

        control = self.fixture.control(decorate=decorate)
        self.assertTrue(settled.wait(1))
        self.fixture.stop_request()
        self.assertTrue(self.client.interrupted.wait(1))
        control.stop_and_join()
        self.assertEqual(attempts, 2)
        self.assertEqual(len(self.client.steers), 1)
        self.assertEqual(
            self.state.get_provider_job(child).status, "cancelled" if rejection else "completed"
        )

    def test_accepted_settlement_busy_retries_only_its_state_write(self) -> None:
        self.assert_transient_settlement(rejection=False)

    def test_rejection_settlement_busy_retries_only_its_state_write(self) -> None:
        self.assert_transient_settlement(rejection=True)

    def test_factory_failure_after_start_returns_definitely_unsent_child_to_queue(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        failed = threading.Event()

        def factory():
            failed.set()
            raise OSError("Example control initialization failed")

        control = self.fixture.control(client_factory=factory)
        self.assertTrue(failed.wait(2))
        control.stop_and_join()
        self.assertEqual(self.client.steers, [])
        self.assertEqual(self.state.get_provider_job(child).status, "queued")
        with self.assertRaises(CodexLiveControlError):
            control.raise_deferred_failure()

    def test_state_close_failure_after_interrupt_does_not_replace_stop_provenance(self) -> None:
        request = self.fixture.stop_request()

        def decorate(state):
            close = state.close

            def close_then_fail():
                close()
                raise OSError("Example close reporting failure")

            state.close = close_then_fail

        with patch("hermes_codex_router.codex_live_control.survived") as diagnostic:
            control = self.fixture.control(decorate=decorate)
            self.assertTrue(self.client.interrupted.wait(2))
            control.stop_and_join()
        self.assertEqual(control.confirmed_interrupt_request, request)
        self.assertIn(
            "codex_live_control.state_close", [call.args[0] for call in diagnostic.call_args_list]
        )

    def test_busy_start_then_success_invokes_child_exactly_once(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        control = self.direct_control()
        with patch.object(self.state, "start_steer_followup", side_effect=fixtures.busy()):
            with self.assertRaises(sqlite3.OperationalError):
                control._steer(self.state)
        self.assertEqual(self.state.get_provider_job(child).status, "leased")
        self.assertEqual(self.client.steers, [])
        control._steer(self.state)
        control._steer(self.state)
        self.assertEqual(self.state.get_provider_job(child).status, "completed")
        self.assertEqual(len(self.client.steers), 1)

    def test_shutdown_retries_only_busy_unstarted_lease_release(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        control = self.direct_control()
        with patch.object(self.state, "start_steer_followup", side_effect=fixtures.busy()):
            with self.assertRaises(sqlite3.OperationalError):
                control._steer(self.state)
        with patch.object(self.state, "release_provider_job_lease", side_effect=fixtures.busy()):
            with self.assertRaises(sqlite3.OperationalError):
                control._settle_pending(self.state, stopping=True)
        self.assertEqual(self.state.get_provider_job(child).status, "leased")
        control._settle_pending(self.state, stopping=True)
        self.assertEqual(self.state.get_provider_job(child).status, "queued")
        self.assertEqual(self.client.steers, [])
        control.raise_deferred_failure()

    def test_busy_unknown_marking_retries_state_and_never_resends_rpc(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        self.client.error = EOFError("Example unknown native outcome")
        control = self.direct_control()
        with patch.object(
            self.state, "mark_provider_job_indeterminate", side_effect=fixtures.busy()
        ):
            with self.assertRaises(sqlite3.OperationalError):
                control._steer(self.state)
        control._steer(self.state)
        control._steer(self.state)
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")
        self.assertEqual(len(self.client.steers), 1)

    def test_permanent_unknown_marking_failure_keeps_stop_available(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        self.client.error = EOFError("Example unknown native outcome")
        failed = threading.Event()
        original = fixtures.busy(sqlite3.SQLITE_IOERR)

        def decorate(state):
            def mark(*args, **kwargs):
                failed.set()
                raise original

            state.mark_provider_job_indeterminate = mark

        control = self.fixture.control(decorate=decorate)
        self.assertTrue(failed.wait(2))
        self.fixture.stop_request()
        self.assertTrue(self.client.interrupted.wait(1))
        control.stop_and_join()
        with self.assertRaises(CodexLiveControlError) as raised:
            control.raise_deferred_failure()
        self.assertIs(raised.exception.__cause__, original)
        self.assertEqual(self.state.get_provider_job(child).status, "executing")
        self.assertEqual(len(self.client.steers), 1)

    def assert_original_completion_failure_retained(self, *, marking_permanent: bool) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        later_child = self.fixture.fixture.enqueue(3, "Example later follow-up")
        original = fixtures.busy(sqlite3.SQLITE_IOERR)
        polls_after_marking, writes = 0, 0
        available = threading.Event()

        def decorate(state):
            original_mark = state.mark_provider_job_indeterminate
            original_lookup = state.pending_emergency_stop_for_job

            def fail_complete(*args, **kwargs):
                raise original

            def mark(*args, **kwargs):
                nonlocal writes
                writes += 1
                if marking_permanent:
                    raise StateError("Example permanent uncertainty marking failure")
                if writes == 1:
                    raise fixtures.busy()
                return original_mark(*args, **kwargs)

            def lookup(*args, **kwargs):
                nonlocal polls_after_marking
                if writes >= (1 if marking_permanent else 2):
                    polls_after_marking += 1
                    if polls_after_marking >= 3:
                        available.set()
                return original_lookup(*args, **kwargs)

            state.complete_steered_job = fail_complete
            state.mark_provider_job_indeterminate = mark
            state.pending_emergency_stop_for_job = lookup

        control = self.fixture.control(decorate=decorate)
        self.assertTrue(available.wait(2))
        request = self.fixture.stop_request()
        self.assertTrue(self.client.interrupted.wait(1))
        control.stop_and_join()
        with self.assertRaises(CodexLiveControlError) as raised:
            control.raise_deferred_failure()
        self.assertIs(raised.exception.__cause__, original)
        self.assertEqual(control.confirmed_interrupt_request, request)
        self.assertEqual(writes, 1 if marking_permanent else 2)
        self.assertEqual(
            self.state.get_provider_job(child).status,
            "executing" if marking_permanent else "indeterminate",
        )
        self.assertEqual(self.state.get_provider_job(later_child).attempt_count, 0)
        self.assertEqual(len(self.client.steers), 1)

    def test_original_completion_fault_survives_busy_then_successful_unknown_marking(self) -> None:
        self.assert_original_completion_failure_retained(marking_permanent=False)

    def test_original_completion_fault_survives_permanent_unknown_marking(self) -> None:
        self.assert_original_completion_failure_retained(marking_permanent=True)

    def test_shutdown_during_post_start_factory_requeues_unsent_child(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        acquired, release = threading.Event(), threading.Event()

        def factory():
            acquired.set()
            release.wait(2)
            return self.client

        control = self.fixture.control(client_factory=factory)
        self.assertTrue(acquired.wait(2))
        self.assertEqual(self.state.get_provider_job(child).status, "executing")
        with self.assertRaises(CodexLiveControlError):
            control.stop_and_join(timeout=0.01)
        release.set()
        control.stop_and_join()
        control.raise_deferred_failure()
        self.assertEqual(self.state.get_provider_job(child).status, "queued")
        self.assertEqual(self.state.get_provider_job(child).attempt_count, 0)
        self.assertEqual(self.client.steers, [])

    def test_busy_unsent_settlement_never_becomes_factory_failure(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        control = self.direct_control()
        with (
            patch.object(control, "_acquire_client", return_value=None),
            patch.object(
                self.state, "reject_unaccepted_steer", side_effect=fixtures.busy()
            ) as write,
        ):
            with self.assertRaises(sqlite3.OperationalError):
                control._steer(self.state)
            write.assert_called_once()
        control._steer(self.state)
        control.raise_deferred_failure()
        self.assertEqual(self.state.get_provider_job(child).status, "queued")
        self.assertEqual(self.client.steers, [])

    def test_replaced_start_token_cannot_start_or_release_new_owner(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        control = self.direct_control()
        with patch.object(self.state, "start_steer_followup", side_effect=fixtures.busy()):
            with self.assertRaises(sqlite3.OperationalError):
                control._steer(self.state)
        old = self.state.get_provider_job(child)
        assert old.lease_token is not None
        self.state.release_provider_job_lease(child, old.lease_token)
        replacement = self.state.lease_steer_followup(self.fixture.job.job_id, "example-new-owner")
        assert replacement is not None
        self.assertNotEqual(replacement.lease_token, old.lease_token)
        with self.assertRaises(StateError):
            control._steer(self.state)
        control._settle_pending(self.state, stopping=True)
        self.assertEqual(self.state.get_provider_job(child).lease_token, replacement.lease_token)
        self.assertEqual(self.client.steers, [])

    def assert_expired_settlement_refuses_replay(self, *, rejection: bool) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        if rejection:
            self.client.error = RpcRejectedError("Example rejected steering")
        control = self.direct_control()
        method = "reject_unaccepted_steer" if rejection else "complete_steered_job"
        with patch.object(self.state, method, side_effect=fixtures.busy()):
            with self.assertRaises(sqlite3.OperationalError):
                control._steer(self.state)
        self.state.recover_stale_provider_jobs(
            now=datetime.now(timezone.utc) + timedelta(minutes=3)
        )
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")
        with self.assertRaises(StateError):
            control._steer(self.state)
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")
        self.assertEqual(len(self.client.steers), 1)

    def test_expired_rejection_token_cannot_requeue_uncertain_child(self) -> None:
        self.assert_expired_settlement_refuses_replay(rejection=True)

    def test_expired_completion_token_cannot_absorb_uncertain_child(self) -> None:
        self.assert_expired_settlement_refuses_replay(rejection=False)

    def test_terminal_parent_cannot_absorb_delayed_accepted_child(self) -> None:
        child = self.fixture.fixture.enqueue(2, "Example follow-up")
        control = self.direct_control()
        with patch.object(self.state, "complete_steered_job", side_effect=fixtures.busy()):
            with self.assertRaises(sqlite3.OperationalError):
                control._steer(self.state)
        parent = self.fixture.job
        assert parent.lease_token is not None
        self.state.mark_provider_job_indeterminate(
            parent.job_id,
            parent.lease_token,
            error_code="example",
            error_detail="Example terminal parent",
        )
        with self.assertRaises(StateError):
            control._steer(self.state)
        control._steer(self.state)
        self.assertEqual(self.state.get_provider_job(child).status, "indeterminate")
        self.assertEqual(len(self.client.steers), 1)


if __name__ == "__main__":
    unittest.main()
