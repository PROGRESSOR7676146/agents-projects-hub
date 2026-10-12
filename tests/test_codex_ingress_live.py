"""Live ingress precautions never replace mandatory progress/result consumption."""

from __future__ import annotations

import sqlite3
import threading
import time
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import StoredTurnOutcome, TurnResult
from hermes_codex_router.codex_execution_control import wait_for_controlled_codex_turn
from hermes_codex_router.codex_ingress_assessments import CodexIngressAssessments
from hermes_codex_router.codex_ingress_control import CodexIngressControl
from hermes_codex_router.codex_live_control import CodexLiveControl
from hermes_codex_router.codex_recovery import checkpoint_failure_notice, reconcile_codex_completion
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.worker_execution import ProviderTurnStopped
from tests import test_telegram_turn_provenance as fixtures


class Client:
    def __init__(self, *outcomes: StoredTurnOutcome) -> None:
        self.outcomes = iter(outcomes or (StoredTurnOutcome("active"), StoredTurnOutcome("active")))
        self.calls: list[str] = []

    def read_turn_outcome(self, **kwargs) -> StoredTurnOutcome:
        self.calls.append("read")
        return next(self.outcomes)

    def interrupt_turn(self, **kwargs) -> None:
        self.calls.append("interrupt")

    def steer_turn(self, **kwargs) -> str:
        raise AssertionError("Example peer received unexpected steering")

    def close(self) -> None:
        self.calls.append("close")


class CodexIngressLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramTurnProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        job, _ = self.fixture.enqueue("hub")
        _, self.token = self.fixture.accept(job)
        self.job = self.state.get_provider_job(job.job_id)
        self.now = datetime.now(timezone.utc)
        self.started = self.now - timedelta(seconds=31)
        self.publisher = self.state.telegram_ingress.register(
            "hub", instance_token="example-live-ingress", previous_epoch=0, now=self.started
        )
        for sequence in (1, 2, 3):
            self.state.telegram_ingress.record_poll(
                self.publisher, sequence=sequence, succeeded=False, observed_at=self.started
            )
        self.client = Client()
        self.acquisitions: list[bool] = []
        self.closed_primary: list[bool] = []
        self.control = CodexLiveControl(
            config=self.fixture.harness.config,
            state_factory=lambda: self.state,
            client_factory=self.acquire,
            job=self.job,
            worker_id="example-ingress-worker",
            thread_id="example-thread",
            turn_id="example-turn",
            transport_mode="socket",
            close_owned_turn_client=lambda: self.closed_primary.append(True),
        )

    def acquire(self):
        self.acquisitions.append(True)
        return self.client

    def checkpoint(self):
        row = ExecutionJournal(self.state).read(self.job.job_id)
        assert row is not None
        return row

    def root(self):
        root = self.checkpoint()["project_root"]
        assert isinstance(root, str)
        return Path(root)

    def test_due_ingress_interrupts_exact_active_turn_once_without_owner_stop(self):
        self.assertTrue(self.control._poll_ingress(self.state))
        self.assertEqual(self.client.calls, ["read", "interrupt", "read", "close"])
        self.assertEqual(self.closed_primary, [True])
        self.assertIsNotNone(self.state.codex_ingress_control.read_cause(self.job.job_id))
        self.assertIsNone(self.state.pending_emergency_stop_for_job(self.job.job_id))
        self.assertIsNone(self.control.confirmed_interrupt_request)
        notices = self.state._connection.execute(
            "SELECT telegram_html FROM task_lifecycle_notices WHERE kind='codex_ingress_control'"
        ).fetchall()
        self.assertEqual(len(notices), 1)
        self.assertIn("reserved", notices[0][0])
        self.assertIn("Telegram ingress", notices[0][0])
        self.assertNotIn("Stop confirmed", notices[0][0])
        notice = checkpoint_failure_notice(self.state, self.job.job_id, EOFError("Example closed"))
        self.assertIn("Telegram ingress", notice)
        with patch(
            "hermes_codex_router.codex_live_control.time.monotonic",
            return_value=self.control._next_ingress_assessment + 1,
        ):
            self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(self.client.calls.count("interrupt"), 1)

    def test_recovered_ingress_keeps_primary_and_never_acquires_control(self):
        self.state.telegram_ingress.record_poll(
            self.publisher, sequence=4, succeeded=True, observed_at=self.now
        )
        self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(self.acquisitions, [])
        self.assertEqual(self.closed_primary, [])

    def _unknown_progress(self):
        journal = ExecutionJournal(self.state, progress_enabled=True)
        journal.record_item(
            self.job.job_id,
            self.token,
            "example-unknown-progress",
            "Example progress",
            "commentary",
        )
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_progress_deliveries SET status='unknown' WHERE job_id=?",
                (self.job.job_id,),
            )
        return self.state._connection.execute(
            "SELECT progress_id FROM provider_progress_deliveries WHERE job_id=?",
            (self.job.job_id,),
        ).fetchone()[0]

    def test_unknown_commentary_alone_with_healthy_ingress_does_not_interrupt(self):
        self._unknown_progress()
        self.state.telegram_ingress.record_poll(
            self.publisher, sequence=4, succeeded=True, observed_at=self.now
        )
        self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.closed_primary, [])
        self.assertIsNone(self.state.codex_ingress_control.read_cause(self.job.job_id))

    def test_delivery_consent_does_not_suppress_due_ingress_or_confirm_progress(self):
        progress_id = self._unknown_progress()
        preview = self.state.preview_delivery_control("progress_delivery", progress_id)
        self.state.reconcile_delivery_control(
            "progress_delivery",
            progress_id,
            expected_snapshot=preview.snapshot,
            accept_unconfirmed_delivery=True,
        )
        self.assertTrue(self.control._poll_ingress(self.state))
        self.assertEqual(self.client.calls.count("interrupt"), 1)
        self.assertIsNotNone(self.state.codex_ingress_control.read_cause(self.job.job_id))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT status FROM provider_progress_deliveries WHERE progress_id=?",
                (progress_id,),
            ).fetchone()[0],
            "unknown",
        )

    def test_optional_assessment_fault_keeps_primary_and_throttles_recheck(self):
        with patch.object(
            self.state.telegram_ingress_assessments,
            "assess",
            side_effect=sqlite3.OperationalError("Example optional assessment fault"),
        ) as assess:
            self.assertFalse(self.control._poll_ingress(self.state))
            self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(assess.call_count, 1)
        self.assertEqual(self.acquisitions, [])
        self.assertEqual(self.closed_primary, [])
        self.assertIsNone(self.control._failure)

    def test_failed_acquisition_is_throttled_before_attempt_without_closing_primary(self):
        self.control.client_factory = lambda: self.fail_acquisition()
        clock = [time.monotonic()]
        with patch(
            "hermes_codex_router.codex_live_control.time.monotonic", side_effect=lambda: clock[0]
        ):
            self.assertFalse(self.control._poll_ingress(self.state))
            clock[0] += 10
            self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(self.acquisitions, [True])
        self.assertEqual(self.closed_primary, [])
        self.assertIsNone(self.control._failure)

    def fail_acquisition(self):
        self.acquisitions.append(True)
        raise OSError("Example optional owning connection unavailable")

    def test_recovery_during_observation_refuses_send_and_keeps_primary(self):
        original_read = self.client.read_turn_outcome

        def recover(**kwargs):
            self.state.telegram_ingress.record_poll(
                self.publisher, sequence=4, succeeded=True, observed_at=datetime.now(timezone.utc)
            )
            return original_read(**kwargs)

        self.client.read_turn_outcome = recover
        self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(self.client.calls, ["read", "close"])
        self.assertEqual(self.closed_primary, [])
        self.assertIsNone(self.state.codex_ingress_control.read_cause(self.job.job_id))

    def test_exact_completed_result_is_saved_without_interrupt_or_stop(self):
        self.client = Client(
            StoredTurnOutcome("completed", TurnResult("Example exact final", None, None))
        )
        self.assertTrue(self.control._poll_ingress(self.state))
        self.assertEqual(self.client.calls, ["read", "close"])
        checkpoint = self.fixture.harness.service.state._connection.execute(
            "SELECT completed_text FROM provider_execution_checkpoints WHERE job_id=?",
            (self.job.job_id,),
        ).fetchone()
        self.assertEqual(checkpoint[0], "Example exact final")
        self.assertIsNone(self.state.pending_emergency_stop_for_job(self.job.job_id))

    def test_unknown_transport_mode_never_grants_ingress_control(self):
        for mode in (None, "stdio-fallback"):
            with self.subTest(mode=mode):
                self.control.transport_mode = mode
                self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(self.acquisitions, [])
        self.assertEqual(self.closed_primary, [])

    def test_stop_arriving_inside_ingress_reservation_reuses_proof_and_real_stop(self):
        original = self.state.codex_ingress_control.begin_interrupt
        requests = []

        def reserve(**kwargs):
            request, _, _ = self.state.request_emergency_stop(
                topic_id=self.job.topic_id,
                chat_id=self.job.chat_id,
                message_id=99,
                target_agent_id="codex",
            )
            requests.append(request)
            return original(**kwargs)

        with patch.object(self.state.codex_ingress_control, "begin_interrupt", side_effect=reserve):
            self.assertTrue(self.control._poll_ingress(self.state))
        target = fixtures.row_values(self.state.codex_controls.read(self.job.job_id))
        self.assertEqual(self.client.calls.count("interrupt"), 1)
        self.assertEqual(target["interrupt_source"], "live")
        self.assertEqual(self.control.confirmed_interrupt_request, requests[0])
        self.assertIsNone(self.state.codex_ingress_control.read_cause(self.job.job_id))

    def test_post_commit_proof_expiry_keeps_primary_and_permanent_no_send_fence(self):
        original = self.state.codex_ingress_control.begin_interrupt
        clock = [time.monotonic()]

        def reserve(**kwargs):
            owner = original(**kwargs)
            clock[0] += 6
            return owner

        with (
            patch(
                "hermes_codex_router.codex_control_recovery.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch.object(self.state.codex_ingress_control, "begin_interrupt", side_effect=reserve),
        ):
            self.assertFalse(self.control._poll_ingress(self.state))
        target = fixtures.row_values(self.state.codex_controls.read(self.job.job_id))
        self.assertEqual(self.client.calls, ["read", "read", "close"])
        self.assertEqual(target["interrupt_outcome"], "not_sent")
        self.assertIsNotNone(target["send_started_at"])
        self.assertIsNotNone(target["owner_quiesced_at"])
        self.assertEqual(self.closed_primary, [])
        fence = dict(target)
        with patch(
            "hermes_codex_router.codex_live_control.time.monotonic",
            return_value=self.control._next_ingress_observation + 1,
        ):
            self.assertFalse(self.control._poll_ingress(self.state))
        self.assertEqual(
            fixtures.row_values(self.state.codex_controls.read(self.job.job_id)), fence
        )
        self.assertEqual(self.client.calls, ["read", "read", "close"])

        child, _ = self.fixture.enqueue("hub")
        self.client.steer_turn = lambda **kwargs: kwargs["turn_id"]
        with (
            patch.object(self.control._stop, "wait", side_effect=[False, True]),
            patch.object(self.state, "close"),
        ):
            self.control._run()
        self.assertEqual(self.state.get_provider_job(child.job_id).status, "completed")
        request, _, _ = self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        with (
            patch.object(self.control._stop, "wait", return_value=False),
            patch.object(self.state, "close"),
            patch.object(self.control, "_steer") as steer,
        ):
            self.control._run()
        steer.assert_not_called()
        self.assertEqual(self.state.pending_emergency_stop_for_job(self.job.job_id), request)
        self.assertNotIn("interrupt", self.client.calls)
        ExecutionJournal(self.state).record_completion(
            self.job.job_id, self.token, "Example final retained after no-send"
        )
        self.assertEqual(
            self.checkpoint()["completed_text"],
            "Example final retained after no-send",
        )
        with self.assertRaises(ProviderTurnStopped):
            reconcile_codex_completion(
                self.state,
                self.fixture.harness.config,
                project_root=self.root(),
                job_id=self.job.job_id,
                lease_token=self.token,
                agent_id="codex",
                client_factory=lambda: cast(Any, self.client),
            )
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "cancelled")

    def _post_attempt_contention(self, fault):
        child, _ = self.fixture.enqueue("hub")
        self.client = Client(
            StoredTurnOutcome("active"),
            StoredTurnOutcome("completed", TurnResult("Example exact final", None, None)),
        )
        original_read = self.state.codex_controls.read
        original_completion = ExecutionJournal.record_completion
        hit = []

        def busy():
            error = sqlite3.OperationalError("Example post-attempt contention")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            return error

        def read(job_id):
            if fault == "read" and self.client.calls.count("read") == 2:
                hit.append(True)
                raise busy()
            return original_read(job_id)

        def completion(journal, *args, **kwargs):
            if fault == "completion":
                hit.append(True)
                raise busy()
            return original_completion(journal, *args, **kwargs)

        with (
            patch.object(self.control._stop, "wait", side_effect=[False, True]),
            patch.object(self.state, "close"),
            patch.object(self.state.codex_controls, "read", side_effect=read),
            patch.object(
                ExecutionJournal,
                "record_completion",
                new=completion,
            ),
            patch.object(self.control, "_steer") as steer,
        ):
            self.control._run()
        self.assertEqual(hit, [True])
        steer.assert_not_called()
        self.assertEqual(self.client.calls.count("interrupt"), 1)
        self.assertEqual(self.closed_primary, [True])
        self.assertEqual(self.state.get_provider_job(child.job_id).status, "queued")
        self.assertIsNotNone(fixtures.row_values(original_read(self.job.job_id))["send_started_at"])
        self.assertIsNone(self.control._failure)

    def test_post_attempt_read_contention_ends_observer_before_steering(self):
        self._post_attempt_contention("read")

    def test_post_attempt_completion_contention_ends_observer_before_steering(self):
        self._post_attempt_contention("completion")

    def _mandatory_flow(self, fault):
        """Run the actual accepted worker wait/control boundary and callbacks."""
        child, _ = self.fixture.enqueue("hub")
        steered = threading.Event()
        assessed = threading.Event()
        original_assess = CodexIngressAssessments.assess

        def assess(domain, *args, **kwargs):
            assessed.set()
            if fault == "assessment":
                raise sqlite3.OperationalError("Example optional assessment failure")
            return original_assess(domain, *args, **kwargs)

        class Peer(Client):
            def read_turn_outcome(self, **kwargs):
                self.calls.append("read")
                if fault == "read":
                    raise OSError("Example optional read failure")
                return StoredTurnOutcome("unknown")

            def steer_turn(self, **kwargs):
                self.calls.append("steer")
                steered.set()
                return kwargs["turn_id"]

        testcase = self

        class Primary:
            on_visible_item: Callable[[str, str, str], None] | None = None
            on_completed: Callable[[TurnResult], None] | None = None

            def close(self):
                testcase.closed_primary.append(True)

            def wait_for_turn(self, turn_id):
                assert self.on_visible_item is not None and self.on_completed is not None
                self.on_visible_item("example-progress", "Example mandatory progress", "commentary")
                testcase.assertTrue(assessed.wait(3))
                testcase.assertTrue(steered.wait(3))
                result = TurnResult("Example mandatory final", 1000, 100)
                self.on_completed(result)
                return result

        peer = Peer()
        journal = ExecutionJournal(self.state, progress_enabled=True)
        notice_patch = (
            patch.object(
                CodexIngressControl,
                "prepare_notice",
                side_effect=RuntimeError("Example notice failure"),
            )
            if fault == "notice"
            else patch.object(CodexIngressControl, "prepare_notice", autospec=True)
        )
        with patch.object(CodexIngressAssessments, "assess", new=assess), notice_patch as notice:
            result = wait_for_controlled_codex_turn(
                cast(Any, Primary()),
                self.state,
                self.fixture.harness.config,
                journal=journal,
                job=self.job,
                worker_id="example-worker",
                thread_id="example-thread",
                turn_id="example-turn",
                transport_mode="socket",
                client_factory=lambda: peer,
            )
        self.assertEqual(result.text, "Example mandatory final")
        self.assertEqual(self.checkpoint()["completed_text"], result.text)
        self.assertEqual(self.state.get_provider_job(child.job_id).status, "completed")
        self.assertEqual(peer.calls.count("steer"), 1)
        if fault == "read":
            self.assertIn("read", peer.calls)
        if fault == "notice":
            notice.assert_called()
        self.assertNotIn("interrupt", peer.calls)
        self.assertEqual(self.closed_primary, [])
        rows = self.state._connection.execute(
            "SELECT visible_text FROM provider_visible_items WHERE job_id=?", (self.job.job_id,)
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["Example mandatory progress"])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM provider_progress_deliveries WHERE job_id=?",
                (self.job.job_id,),
            ).fetchone()[0],
            1,
        )

    def test_assessment_failure_preserves_worker_progress_steering_and_final(self):
        self._mandatory_flow("assessment")

    def test_read_failure_preserves_worker_progress_steering_and_final(self):
        self._mandatory_flow("read")

    def test_notice_failure_preserves_worker_progress_steering_and_final(self):
        self._mandatory_flow("notice")
