"""Independent connections, commit failures and controls cannot fabricate acceptance."""

from __future__ import annotations

import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.assessment_inputs import OutcomeAssessmentInput
from hermes_codex_router.codex_appserver import (
    CodexTurnError,
    RpcError,
    StoredTurnOutcome,
    TurnResult,
)
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.task_notice_sender import deliver_task_notice
from hermes_codex_router.telegram import TelegramError
from hermes_codex_router.turn_observation import TurnObservation
from tests import test_codex_worker as worker_fixtures
from tests.delivery_fixture import complete_final_delivery
from tests.test_outcome_assessment_state import OutcomeAssessmentFixture
from tests.test_task_notice_sender import FakeTelegram


class AssessedFinalRetentionTests(unittest.TestCase):
    def test_recovered_assessed_final_cannot_reenter_notice_replacement(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        class Client(worker_fixtures.WorkerClient):
            observed = "unknown"
            reads = 0

            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise CodexTurnError(RpcError("Example lost stream"), "Example partial")

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                self.reads += 1
                return StoredTurnOutcome(
                    cast(Any, self.observed),
                    TurnResult("Example recovered result", None, None)
                    if self.observed == "completed"
                    else None,
                )

        job_id = fixture.enqueue(1, "Example task")
        client = Client()
        worker = fixture.worker(client)
        self.addCleanup(worker.close)
        worker.run_cycle()
        state = worker.state
        job = state.get_provider_job(job_id)
        self.assertEqual(job.status, "indeterminate")
        observation = TurnObservation(state, fixture.config)
        client.observed = "completed"
        self.assertTrue(observation.observe_topic(job.topic_id, cast(Any, lambda: client)))
        self.assertEqual(state.get_provider_job(job_id).status, "result_ready")
        outbox = state.lease_telegram_outbox("codex", "example-sender")
        assert outbox is not None and outbox.lease_token is not None
        complete_final_delivery(
            state, outbox.outbox_id, outbox.lease_token, telegram_message_id=101
        )
        disposition, created = state.record_outcome_assessment(
            OutcomeAssessmentInput(
                42, job.chat_id, 77, 50, 101, "/assess accepted Example checked"
            ),
            project_id="example-project",
            owner_user_ids=(42,),
        )
        self.assertTrue(created)
        self.assertEqual(disposition.disposition, "applied")
        self.assertEqual(state.get_provider_job(job_id).status, "completed")
        before = "\n".join(state._connection.iterdump())
        reads = client.reads
        self.assertFalse(observation.observe_topic(job.topic_id, cast(Any, lambda: client)))
        self.assertFalse(observation.run_once(cast(Any, lambda: client)))
        with self.assertRaisesRegex(StateError, "observed turn binding changed"):
            observation._commit_terminal(
                job_id,
                "thread-1",
                "turn-1",
                fixture.registry.projects[0].root,
                StoredTurnOutcome(
                    "completed", TurnResult("Example repeated observation", None, None)
                ),
            )
        self.assertEqual(client.reads, reads)
        self.assertEqual(client.turns, 1)
        self.assertEqual("\n".join(state._connection.iterdump()), before)


class OutcomeAssessmentBoundaryTests(OutcomeAssessmentFixture):
    def queued(self, number: int, *, topic=None, status="queued", held=False):
        topic = topic or self.fixture.topic
        session = self.state.get_session(self.fixture.session.session_id)
        if topic.topic_id != session.topic_id:
            session = self.state.activate_agent(topic.topic_id, "codex", "example-model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key=f"example-boundary:{number}",
            chat_id=topic.chat_id,
            message_id=number,
            topic_id=topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Example already accepted input",
        )
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status=?,next_attempt_at='2099-01-01T00:00:00+00:00' WHERE job_id=?",
                (status, job.job_id),
            )
            if held:
                self.state._connection.execute(
                    "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at) VALUES (?,?,'2026-01-01')",
                    (job.job_id, self.job.job_id),
                )
        return self.state.get_provider_job(job.job_id)

    def test_control_boundary_is_atomic_and_duplicate_cannot_flush_later_work(self) -> None:
        self.deliver()
        queued = self.queued(2)
        held = self.queued(3, held=True)
        retry_wait = self.queued(4, status="retry_wait")
        other_topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=self.fixture.topic.chat_id,
            thread_id=78,
            title="Example other topic",
        )
        other = self.queued(5, topic=other_topic)
        first, _ = self.assess(self.request())
        self.assertEqual(
            self.state.get_provider_job(queued.job_id).next_attempt_at, first.created_at
        )
        self.assertEqual(self.state.get_provider_job(held.job_id).next_attempt_at, first.created_at)
        self.assertEqual(self.state.held_provider_job_count(self.fixture.topic.topic_id), 1)
        self.assertEqual(self.state.get_provider_job(retry_wait.job_id), retry_wait)
        self.assertEqual(self.state.get_provider_job(other.job_id), other)
        later = self.queued(6)
        self.assertEqual(self.assess(self.request()), (first, False))
        with self.assertRaises(StateError):
            self.assess(replace(self.request(), text="/assess rework Example changed"))
        self.assertEqual(self.state.get_provider_job(later.job_id), later)
        refusal, _ = self.assess(self.request(51, text="/assess invalid Example"))
        self.assertEqual(refusal.disposition, "refused")
        self.assertEqual(
            self.state.get_provider_job(later.job_id).next_attempt_at, refusal.created_at
        )

    def test_fault_after_flush_rolls_back_journal_receipt_notice_and_deadline(self) -> None:
        self.deliver()
        queued = self.queued(2)
        before = "\n".join(self.state._connection.iterdump())
        original = self.state._provider_job_state.flush_batch_in_transaction

        def fault(*args):
            original(*args)
            raise sqlite3.OperationalError("Example after flush")

        with patch.object(
            self.state._provider_job_state, "flush_batch_in_transaction", side_effect=fault
        ):
            with self.assertRaises(sqlite3.Error):
                self.assess(self.request())
        self.assertEqual("\n".join(self.state._connection.iterdump()), before)
        self.assertEqual(self.state.get_provider_job(queued.job_id), queued)
        self.assertEqual(self.assess(self.request())[0].disposition, "applied")

    def test_real_commit_denial_rolls_back_and_input_can_be_retried(self) -> None:
        self.deliver()
        before = "\n".join(self.state._connection.iterdump())

        def authorizer(action, first, _second, _database, _source):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
                else sqlite3.SQLITE_OK
            )

        self.state._connection.set_authorizer(authorizer)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.assess(self.request())
        finally:
            self.state._connection.set_authorizer(None)
        self.assertFalse(self.state._connection.in_transaction)
        self.assertEqual("\n".join(self.state._connection.iterdump()), before)
        self.assertEqual(self.assess(self.request())[0].disposition, "applied")

    def race(self, requests):
        barrier = threading.Barrier(2)

        def worker(request):
            state = HubState.open(self.fixture.path, codex_permission_profile=None)
            try:
                barrier.wait(timeout=5)
                return state.record_outcome_assessment(
                    request, project_id="example-project", owner_user_ids=(42,)
                )[0]
            finally:
                state.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(worker, requests))

    def test_first_and_correction_races_have_one_applied_successor(self) -> None:
        self.deliver()
        outcomes = self.race((self.request(50), self.request(51, text="/assess rework Example")))
        self.assertEqual(sorted(x.disposition for x in outcomes), ["applied", "refused"])
        first = next(x for x in outcomes if x.disposition == "applied")
        corrections = self.race(
            (
                self.request(52, reply=first.input_message_id, text="/assess unknown Example"),
                self.request(53, reply=first.input_message_id, text="/assess rework Example"),
            )
        )
        self.assertEqual(sorted(x.disposition for x in corrections), ["applied", "refused"])
        latest = next(x for x in corrections if x.disposition == "applied")
        self.assertEqual((latest.revision, latest.predecessor_id), (2, first.disposition_id))
        self.assertEqual(
            self.fixture.read(self.job.job_id)["acceptance"]["disposition_id"],
            latest.disposition_id,
        )

    def test_unknown_ack_survives_restart_no_resend_and_is_not_a_correction_target(self) -> None:
        self.deliver()
        first, _ = self.assess(self.request())
        bot = FakeTelegram(
            error=TelegramError(
                "Example timeout", operation="send_message", failure_class="network_timeout"
            )
        )
        result = deliver_task_notice(self.state.task_notices, bot, "example-sender")
        self.assertIsNotNone(result.error)
        notice_id = self.state._connection.execute(
            "SELECT notice_id FROM task_lifecycle_notices"
        ).fetchone()[0]
        self.state.close()
        self.state = HubState.open(self.fixture.path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.assertEqual(self.state.task_notices.get_notice(notice_id).status, "unknown")
        self.assertFalse(deliver_task_notice(self.state.task_notices, bot, "example-sender").worked)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(self.assess(self.request()), (first, False))
        corrected, _ = self.assess(self.request(51, reply=50, text="/assess rework Example"))
        self.assertEqual(corrected.revision, 2)
        bot = FakeTelegram(receipt=501)
        self.assertTrue(
            deliver_task_notice(self.state.task_notices, bot, "example-sender").delivered
        )
        refused, _ = self.assess(self.request(52, reply=501))
        self.assertEqual(refused.disposition, "refused")

    def test_refusal_notice_retains_own_destination_without_borrowing_a_job(self) -> None:
        record, _ = self.assess(replace(self.request(), thread_id=79))
        self.assertIsNone(record.topic_id)
        now = datetime.now(timezone.utc)
        with self.state._immediate_transaction():
            for changes in ({"chat_id": 43}, {"thread_id": 77}, {"job_id": self.job.job_id}):
                values: dict[str, Any] = dict(
                    event_key="example-tamper",
                    kind="outcome_assessed",
                    assessment_disposition_id=record.disposition_id,
                    chat_id=record.chat_id,
                    thread_id=record.thread_id,
                    telegram_html="Example",
                    now=now,
                )
                values.update(changes)
                with self.assertRaises(StateError):
                    self.state.task_notices.prepare_notice_in_transaction(**values)
        bot = FakeTelegram()
        self.assertTrue(
            deliver_task_notice(self.state.task_notices, bot, "example-sender").delivered
        )
        self.assertEqual(bot.calls[0][:2], (record.chat_id, 79))
