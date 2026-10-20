"""Real worker admission/SQLite fixtures, without provider or service access."""

from __future__ import annotations

import threading
import unittest
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_late_control import CodexControlMaintenance
from tests import test_external_worker as fixtures


class WorkerDrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ExternalQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_drain_before_cycle_prevents_every_state_and_provider_call(self) -> None:
        job_id = self.fixture.enqueue("opencode", 401)
        adapter = fixtures.Adapter("opencode")
        worker = self.fixture.worker("opencode", adapter)
        self.addCleanup(worker.close)
        before = worker.state.get_provider_job(job_id)
        worker.request_drain()
        statements: list[str] = []
        worker.state._connection.set_trace_callback(statements.append)
        try:
            self.assertFalse(worker.run_cycle())
        finally:
            worker.state._connection.set_trace_callback(None)
        self.assertEqual(statements, [])
        self.assertEqual(worker.state.get_provider_job(job_id), before)
        self.assertEqual(adapter.calls, 0)
        self.assertTrue(worker.activity.drained)

    def test_drain_during_productive_turn_preserves_result_and_queued_tail(self) -> None:
        job_id = self.fixture.enqueue("opencode", 402)
        tail = self.fixture.enqueue("opencode", 403)
        adapter = fixtures.Adapter("opencode")
        worker = self.fixture.worker("opencode", adapter)
        self.addCleanup(worker.close)
        original = adapter.run_turn

        def run_turn(**kwargs: object) -> Any:
            worker.request_drain()
            self.assertFalse(worker.activity.drained)
            self.assertFalse(worker._stop.is_set())
            return original(**kwargs)

        with patch.object(adapter, "run_turn", run_turn):
            self.assertTrue(worker.run_cycle())
        self.assertFalse(worker.run_cycle())
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(worker.state.get_provider_job(tail).status, "queued")
        self.assertTrue(worker.activity.drained)

    def test_direct_connect_pass_is_fenced_before_store_or_client_creation(self) -> None:
        worker = self.fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        worker.request_drain()
        with patch(
            "hermes_codex_router.external_worker.SessionConnectStore",
            side_effect=AssertionError("metadata claim must not run"),
        ):
            self.assertFalse(worker._run_connect_cycle())

    def test_late_control_pass_is_fenced_before_claim_or_ingress_scan(self) -> None:
        worker = self.fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        maintenance = CodexControlMaintenance(
            self.fixture.config,
            worker_id="example-worker",
            agent_id="codex",
            client_factory=lambda _: cast(Any, None),
            stop=threading.Event(),
            activity=worker.activity,
        )
        worker.request_drain()
        with (
            patch(
                "hermes_codex_router.codex_late_control.run_late_control_once",
                side_effect=AssertionError("late claim must not run"),
            ),
            patch.object(maintenance.ingress, "run_once", side_effect=AssertionError("ingress")),
        ):
            self.assertFalse(maintenance.run_once(worker.state))

    def test_prestartup_drain_does_not_start_the_supervisor(self) -> None:
        from unittest.mock import Mock

        worker = self.fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        supervisor = Mock()
        worker.supervisor = supervisor
        worker.request_drain()
        worker.run_forever()
        supervisor.start.assert_not_called()
        self.assertTrue(worker.activity.drained)

    def test_connect_thread_retains_activity_through_database_close(self) -> None:
        from hermes_codex_router.state import HubState

        worker = self.fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        entered, closing, finish = threading.Event(), threading.Event(), threading.Event()
        original_open = HubState.open

        def opened(*args: Any, **kwargs: Any) -> HubState:
            state = original_open(*args, **kwargs)
            original_close = state.close

            def close() -> None:
                closing.set()
                finish.wait(2)
                original_close()

            state.close = close  # type: ignore[method-assign]
            return state

        def metadata(**_: Any) -> bool:
            entered.set()
            worker.request_drain()
            return False

        with (
            patch.object(HubState, "open", opened),
            patch.object(worker, "_run_connect_cycle", metadata),
        ):
            thread = worker.activity.thread(target=worker._run_connect_forever)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(closing.wait(2))
                self.assertFalse(worker.activity.drained)
                self.assertFalse(worker._stop.is_set())
            finally:
                finish.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(worker.activity.drained)

    def test_live_control_join_timeout_cannot_report_drained(self) -> None:
        from hermes_codex_router.codex_live_control import CodexLiveControl, CodexLiveControlError

        worker = self.fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        entered, finish = threading.Event(), threading.Event()
        control = CodexLiveControl(
            config=self.fixture.config,
            state_factory=lambda: worker.state,
            client_factory=lambda: cast(Any, None),
            job=cast(Any, None),
            worker_id="example-worker",
            thread_id="example-thread",
            turn_id="example-turn",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
            activity=worker.activity,
        )

        def background() -> None:
            entered.set()
            finish.wait(2)

        with patch.object(control, "_run", background):
            control.start()
        try:
            self.assertTrue(entered.wait(2))
            worker.request_drain()
            with self.assertRaises(CodexLiveControlError):
                control.stop_and_join(timeout=0.001)
            self.assertFalse(worker.activity.drained)
        finally:
            finish.set()
            control.stop_and_join()
        self.assertTrue(worker.activity.drained)


class MaintenanceDrainTests(unittest.TestCase):
    def test_completed_operations_do_not_fabricate_durable_interrupt_settlement(self) -> None:
        from hermes_codex_router.codex_appserver import StoredTurnOutcome
        from hermes_codex_router.codex_turn_controls import CodexTurnControls
        from hermes_codex_router.worker_activity_barrier import WorkerActivityBarrier
        from tests import test_codex_late_control as late

        fixture = late.LateCodexControlTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        activity = WorkerActivityBarrier()
        client = late.Client(StoredTurnOutcome("active"), StoredTurnOutcome("interrupted"))
        maintenance = CodexControlMaintenance(
            fixture.fixture.fixture.config,
            worker_id="example-worker",
            agent_id="codex",
            client_factory=lambda _: cast(Any, client),
            stop=threading.Event(),
            activity=activity,
        )
        with patch.object(
            CodexTurnControls,
            "finish_interrupt",
            side_effect=RuntimeError("fictional storage fault"),
        ):
            self.assertTrue(maintenance.run_once(fixture.state))
        activity.request_drain()
        self.assertTrue(activity.drained)
        self.assertIsNotNone(fixture.row()["send_started_at"])
        self.assertIsNone(fixture.row()["owner_quiesced_at"])
        self.assertEqual([call[0] for call in client.calls].count("interrupt"), 1)

    def test_drain_preserves_inflight_interrupt_and_its_settlement(self) -> None:
        from hermes_codex_router.codex_appserver import StoredTurnOutcome
        from hermes_codex_router.worker_activity_barrier import WorkerActivityBarrier
        from tests import test_codex_late_control as late

        fixture = late.LateCodexControlTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        activity = WorkerActivityBarrier()
        entered, finish, closed = threading.Event(), threading.Event(), threading.Event()
        stop = threading.Event()
        client = late.Client(StoredTurnOutcome("active"), StoredTurnOutcome("interrupted"))

        def interrupt(**kwargs: Any) -> None:
            client.calls.append(("interrupt", kwargs))
            entered.set()
            finish.wait(2)
            if closed.is_set():
                raise EOFError("fictional premature close")

        client.interrupt_turn = interrupt
        client.close = closed.set
        maintenance = CodexControlMaintenance(
            fixture.fixture.fixture.config,
            worker_id="example-worker",
            agent_id="codex",
            client_factory=lambda _: cast(Any, client),
            stop=stop,
            activity=activity,
        )
        thread = activity.thread(target=maintenance.run_forever)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            activity.request_drain()
            self.assertFalse(activity.drained)
            self.assertFalse(stop.is_set())
            self.assertFalse(closed.is_set())
        finally:
            finish.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(activity.drained)
        self.assertEqual(fixture.row()["interrupt_outcome"], "matched_ack")
        self.assertIsNotNone(fixture.row()["owner_quiesced_at"])
        self.assertEqual([call[0] for call in client.calls].count("interrupt"), 1)


if __name__ == "__main__":
    unittest.main()
