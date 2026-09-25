from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import patch

import test_codex_worker as worker_fixtures
import test_embedded_queue_service as embedded_fixtures
import test_outbox_sender as sender_fixtures
from test_codex_appserver import FakeTransport

from hermes_codex_router.codex_appserver import (
    CodexAppServerClient,
    CodexTurnError,
    RpcError,
    StoredTurnOutcome,
    TurnResult,
)
from hermes_codex_router.codex_failure import MAX_PARTIAL_TEXT, codex_failure_notice
from hermes_codex_router.delivery_retry import delivery_retry_delay
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError

WorkerClient = worker_fixtures.WorkerClient
Bot = sender_fixtures.Bot


def item(text: str, *, item_id: str = "answer-1", kind: str = "agentMessage") -> dict:
    return {
        "method": "item/completed",
        "params": {
            "turnId": "turn-1",
            "item": {"id": item_id, "type": kind, "text": text},
        },
    }


def completion(*, failed: bool = False) -> dict:
    return {
        "method": "turn/completed",
        "params": {
            "turn": {
                "id": "turn-1",
                "status": "failed" if failed else "completed",
                "error": {"message": "HTTP 429 Too Many Requests; private diagnostic"}
                if failed
                else None,
            }
        },
    }


class ResultReliabilityTests(unittest.TestCase):
    def test_late_terminal_read_waits_for_current_notice_sender_lease(self) -> None:
        from hermes_codex_router.turn_observation import TurnObservation

        class Client(WorkerClient):
            observed = "unknown"

            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise CodexTurnError(RpcError("fictional SSE disconnect"))

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                return StoredTurnOutcome(cast(Any, self.observed))

        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        old_id = fixture.enqueue()
        client = Client()
        worker = fixture.worker(client)
        try:
            self.assertTrue(worker.run_cycle())
            sending = worker.state.lease_telegram_outbox("codex", "fictional-sender")
            assert sending is not None and sending.lease_token is not None
            client.observed = "failed"
            self.assertTrue(
                TurnObservation(worker.state, fixture.config).run_once(cast(Any, lambda: client))
            )
            self.assertEqual(
                worker.state.get_telegram_outbox_for_job(old_id).outbox_id, sending.outbox_id
            )
            self.assertIsNone(
                worker.state._connection.execute(
                    "SELECT 1 FROM provider_turn_terminal_evidence WHERE job_id = ?", (old_id,)
                ).fetchone()
            )
            worker.state.mark_telegram_outbox_delivered(
                sending.outbox_id, sending.lease_token, telegram_message_id=101
            )
            with worker.state._immediate_transaction():
                worker.state._connection.execute(
                    "UPDATE provider_turn_observations SET next_check_at = ? WHERE job_id = ?",
                    ("2000-01-01T00:00:00+00:00", old_id),
                )
            self.assertTrue(
                TurnObservation(worker.state, fixture.config).run_once(cast(Any, lambda: client))
            )
            self.assertIn(
                "reply exactly retry",
                worker.state.get_telegram_outbox_for_job(old_id).telegram_html,
            )
            self.assertEqual(client.turns, 1)
        finally:
            worker.close()
            fixture.tearDown()

    def test_late_exact_read_reconciles_without_a_second_turn(self) -> None:
        from hermes_codex_router.turn_observation import TurnObservation

        for final_status in ("completed", "failed", "interrupted", "active", "unknown"):
            with self.subTest(final_status=final_status):

                class Client(WorkerClient):
                    observed = "unknown"

                    def wait_for_turn(self, _turn_id: str) -> TurnResult:
                        cast(Any, self).on_visible_item("visible-1", "Partial work", "commentary")
                        raise CodexTurnError(RpcError("fictional SSE disconnect"), "Partial work")

                    def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                        return StoredTurnOutcome(
                            cast(Any, self.observed),
                            TurnResult("Stored final answer", None, None)
                            if self.observed == "completed"
                            else None,
                        )

                fixture = worker_fixtures.CodexQueueWorkerTests()
                fixture.setUp()
                old_id = fixture.enqueue(1, "Fictional task")
                tail_id = None
                if final_status == "failed":
                    tail_state = HubState.open(fixture.config.state_path)
                    old_job = tail_state.get_provider_job(old_id)
                    tail, _ = tail_state.enqueue_provider_job(
                        idempotency_key="telegram:fictional-held-tail",
                        chat_id=old_job.chat_id,
                        message_id=9,
                        topic_id=old_job.topic_id,
                        agent_id="codex",
                        session_id=old_job.session_id,
                        session_generation=old_job.session_generation,
                        model=old_job.model,
                        effort=old_job.effort,
                        payload_text="Earlier fictional queued request",
                    )
                    tail_id = tail.job_id
                    tail_state.close()
                client = Client()
                worker = fixture.worker(client)
                try:
                    self.assertTrue(worker.run_cycle())
                    self.assertEqual(worker.state.get_provider_job(old_id).status, "indeterminate")
                    if final_status == "completed":
                        staging = fixture.registry.projects[0].root / ".hub" / "staging" / old_id
                        staging.mkdir(parents=True, exist_ok=True)
                        (staging / "fictional-result.txt").write_text("saved artifact")
                    first = worker.state.get_telegram_outbox_for_job(old_id)
                    delivery = worker.state.lease_telegram_outbox("codex", "sender")
                    assert delivery is not None and delivery.lease_token is not None
                    worker.state.mark_telegram_outbox_delivered(
                        delivery.outbox_id, delivery.lease_token, telegram_message_id=101
                    )
                    client.observed = final_status
                    reopened = HubState.open(fixture.config.state_path)
                    try:
                        self.assertTrue(
                            TurnObservation(reopened, fixture.config).run_once(
                                cast(Any, lambda: client)
                            )
                        )
                    finally:
                        reopened.close()
                    old = worker.state.get_provider_job(old_id)
                    if final_status == "completed":
                        self.assertEqual(old.status, "result_ready")
                        self.assertEqual(
                            worker.state.get_provider_result(old_id).visible_response,
                            "Stored final answer",
                        )
                    else:
                        self.assertEqual(old.status, "indeterminate")
                    notice = worker.state.get_telegram_outbox_for_job(old_id)
                    if final_status in {"failed", "interrupted"}:
                        self.assertIn("reply exactly retry", notice.telegram_html)
                        if tail_id is not None:
                            self.assertIn(
                                "1 earlier queued request(s) remain paused", notice.telegram_html
                            )
                            held = worker.state._connection.execute(
                                "SELECT 1 FROM provider_job_holds WHERE job_id = ?", (tail_id,)
                            ).fetchone()
                            self.assertIsNotNone(held)
                    elif final_status == "completed":
                        self.assertIn("Stored final answer", notice.telegram_html)
                        self.assertTrue(
                            any(
                                part.part_type == "document"
                                and part.file_name == "fictional-result.txt"
                                for part in worker.state.get_telegram_outbox_parts(notice.outbox_id)
                            )
                        )
                    else:
                        self.assertEqual(first.outbox_id, notice.outbox_id)
                    archived = worker.state._connection.execute(
                        "SELECT outbox_id, telegram_html, telegram_message_id "
                        "FROM provider_recovery_notices WHERE job_id = ?",
                        (old_id,),
                    ).fetchone()
                    if final_status in {"completed", "failed", "interrupted"}:
                        self.assertIsNotNone(archived)
                        assert archived is not None
                        self.assertEqual(archived["outbox_id"], first.outbox_id)
                        self.assertEqual(archived["telegram_html"], first.telegram_html)
                        self.assertEqual(archived["telegram_message_id"], 101)
                    else:
                        self.assertIsNone(archived)
                        for _ in range(2):
                            with worker.state._immediate_transaction():
                                worker.state._connection.execute(
                                    "UPDATE provider_turn_observations SET next_check_at = ? "
                                    "WHERE job_id = ?",
                                    ("2000-01-01T00:00:00+00:00", old_id),
                                )
                            self.assertTrue(
                                TurnObservation(worker.state, fixture.config).run_once(
                                    cast(Any, lambda: client)
                                )
                            )
                        self.assertFalse(
                            TurnObservation(worker.state, fixture.config).run_once(
                                cast(Any, lambda: client)
                            )
                        )
                    self.assertEqual(client.turns, 1)
                finally:
                    worker.close()
                    fixture.tearDown()

    def test_telegram_reply_retry_is_bound_to_delivered_failed_notice(self) -> None:
        class Client(embedded_fixtures.QueueClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                cast(Any, self).on_visible_item("visible-1", "Partial visible work", "commentary")
                raise CodexTurnError(RpcError("fictional SSE disconnect"), "Partial visible work")

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                return StoredTurnOutcome("failed")

        fixture = embedded_fixtures.EmbeddedQueueServiceTests()
        fixture.setUp()
        client = Client()
        service, telegram = fixture.service(client)
        try:
            self.assertTrue(service.handle_update(embedded_fixtures.update(1, "Original task")))
            self.assertTrue(service.run_embedded_queue_cycle())
            self.assertIn("reply exactly retry", telegram.sent[-1])
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            old = service.state.provider_jobs_for_topic(topic.topic_id)[0]
            notice = service.state.get_telegram_outbox_for_job(old.job_id)
            assert notice.telegram_message_id is not None
            reply = embedded_fixtures.update(2, "retry")
            cast(dict[str, Any], reply["message"])["reply_to_message"] = {
                "message_id": notice.telegram_message_id,
                "from": {"is_bot": True, "username": "example_codex_bot"},
            }
            self.assertTrue(service.handle_update(reply))
            jobs = service.state.provider_jobs_for_topic(topic.topic_id)
            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0].status, "indeterminate")
            self.assertEqual(jobs[1].provider_session_id, "thread-1")
            self.assertIn("inspect the current project state", jobs[1].payload_text)
            self.assertEqual(len(client.turn_threads), 1)
            self.assertFalse(service.handle_update(reply))
            self.assertEqual(len(service.state.provider_jobs_for_topic(topic.topic_id)), 2)
        finally:
            service.close()
            fixture.tearDown()

    def test_plain_retry_is_not_a_notice_continuation(self) -> None:
        class Client(embedded_fixtures.QueueClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise CodexTurnError(RpcError("fictional SSE disconnect"))

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                return StoredTurnOutcome("failed")

        fixture = embedded_fixtures.EmbeddedQueueServiceTests()
        fixture.setUp()
        service, _telegram = fixture.service(Client())
        try:
            self.assertTrue(service.handle_update(embedded_fixtures.update(1, "Fictional task")))
            self.assertTrue(service.run_embedded_queue_cycle())
            self.assertTrue(service.handle_update(embedded_fixtures.update(2, "retry")))
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            jobs = service.state.provider_jobs_for_topic(topic.topic_id)
            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[1].payload_text, "retry")
            self.assertIsNone(
                service.state._connection.execute(
                    "SELECT 1 FROM provider_job_continuations LIMIT 1"
                ).fetchone()
            )
        finally:
            service.close()
            fixture.tearDown()

    def test_local_checks_uncertain_turn_without_model_call(self) -> None:
        class Client(embedded_fixtures.QueueClient):
            observed = "unknown"

            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise CodexTurnError(RpcError("fictional SSE disconnect"))

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                return StoredTurnOutcome(cast(Any, self.observed))

        fixture = embedded_fixtures.EmbeddedQueueServiceTests()
        fixture.setUp()
        client = Client()
        service, telegram = fixture.service(client)
        try:
            self.assertTrue(service.handle_update(embedded_fixtures.update(1, "Fictional task")))
            self.assertTrue(service.run_embedded_queue_cycle())
            with patch("hermes_codex_router.service.owning_read_client", return_value=client):
                self.assertTrue(service.handle_update(embedded_fixtures.update(2, "/local")))
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            session = service.state.active_session(topic.topic_id)
            assert session is not None
            self.assertEqual(session.writer_mode, "telegram")
            self.assertIn("active or unconfirmed", telegram.sent[-1])
            client.observed = "failed"
            with patch("hermes_codex_router.service.owning_read_client", return_value=client):
                self.assertTrue(service.handle_update(embedded_fixtures.update(3, "/local")))
            self.assertEqual(service.state.get_session(session.session_id).writer_mode, "local")
            self.assertEqual(len(client.turn_threads), 1)
        finally:
            service.close()
            fixture.tearDown()

    def test_failed_turn_after_partial_notice_offers_executable_continuation(self) -> None:
        """A terminal SSE failure must not tell the owner to send blocked input."""

        class Client(WorkerClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                cast(Any, self).on_visible_item("visible-1", "Partial visible work", "commentary")
                raise CodexTurnError(
                    RpcError("stream disconnected before completion: idle timeout waiting for SSE"),
                    "Partial visible work",
                )

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                return StoredTurnOutcome("failed")

        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        client = Client()
        old_job_id = fixture.enqueue(1, "Original fictional task")
        setup_state = HubState.open(fixture.config.state_path)
        try:
            old = setup_state.get_provider_job(old_job_id)
            tail, _ = setup_state.enqueue_provider_job(
                idempotency_key="telegram:fictional-tail",
                chat_id=old.chat_id,
                message_id=2,
                topic_id=old.topic_id,
                agent_id="codex",
                session_id=old.session_id,
                session_generation=old.session_generation,
                model=old.model,
                effort=old.effort,
                payload_text="Earlier queued fictional follow-up",
                available_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        finally:
            setup_state.close()
        worker = fixture.worker(client)
        try:
            self.assertTrue(worker.run_cycle())
            old_job = worker.state.get_provider_job(old_job_id)
            notice = worker.state.get_telegram_outbox_for_job(old_job_id)
            self.assertEqual(old_job.status, "indeterminate")
            self.assertEqual(
                worker.state.execution_capacity_snapshot(1)["blocked_uncertain_scopes"], 0
            )
            self.assertEqual(
                worker.state.reliability_snapshot()["unresolved_uncertain_execution"], 0
            )
            self.assertIn("Partial visible work", notice.telegram_html)
            self.assertIn("Continue with inspection", notice.telegram_html)
            self.assertIn("1 earlier request(s) remain paused", notice.telegram_html)
            self.assertNotIn("send a new message", notice.telegram_html)
            self.assertEqual(client.turns, 1)
            delivery = worker.state.lease_telegram_outbox("codex", "fictional-sender")
            assert delivery is not None and delivery.lease_token is not None
            worker.state.mark_telegram_outbox_delivered(
                delivery.outbox_id, delivery.lease_token, telegram_message_id=101
            )
            from hermes_codex_router.turn_continuation_state import TurnContinuationState

            continuation = TurnContinuationState(worker.state)
            self.assertEqual(
                continuation.source_for_notice(
                    chat_id=-1001234567890, thread_id=77, notice_message_id=101
                ),
                old_job_id,
            )
            continued, created, held_count = continuation.continue_from_notice(
                source_job_id=old_job_id,
                chat_id=-1001234567890,
                thread_id=77,
                notice_message_id=101,
                reply_message_id=102,
                canonical_root=fixture.registry.projects[0].root,
            )
            self.assertTrue(created)
            self.assertEqual(held_count, 1)
            self.assertEqual(worker.state.get_provider_job(tail.job_id).status, "queued")
            self.assertEqual(continued.provider_session_id, "thread-1")
            self.assertNotEqual(continued.payload_text, "Original fictional task")
            self.assertIn("inspect the current project state", continued.payload_text)
            duplicate, created, _ = continuation.continue_from_notice(
                source_job_id=old_job_id,
                chat_id=-1001234567890,
                thread_id=77,
                notice_message_id=101,
                reply_message_id=103,
                canonical_root=fixture.registry.projects[0].root,
            )
            self.assertFalse(created)
            self.assertEqual(duplicate.job_id, continued.job_id)
            self.assertEqual(worker.state.get_provider_job(old_job_id).status, "indeterminate")
            self.assertEqual(client.turns, 1)
            lease = worker.state.lease_provider_job("codex", "fictional-worker")
            assert lease is not None
            self.assertEqual(lease.job_id, continued.job_id)
        finally:
            worker.close()
            fixture.tearDown()

    def test_completion_buffered_during_rpc_is_consumed_before_transport(self) -> None:
        transport = FakeTransport(
            [
                item("Already finished"),
                completion(),
                {"id": 1, "result": {"data": []}},
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        client.list_models()
        self.assertEqual(client.wait_for_turn("turn-1").text, "Already finished")
        self.assertEqual(len(transport.receive_timeouts), 3)

    def test_duplicate_visible_item_is_not_published_twice(self) -> None:
        client = CodexAppServerClient(
            FakeTransport(
                [
                    item("Answer"),
                    item("Answer"),
                    completion(),
                ]
            ),
            initialized=True,
        )
        self.assertEqual(client.wait_for_turn("turn-1").text, "Answer")

    def test_failure_and_disconnect_retain_only_completed_visible_text(self) -> None:
        for tail in ([completion(failed=True)], []):
            with self.subTest(disconnect=not tail):
                client = CodexAppServerClient(
                    FakeTransport(
                        [
                            item("Hidden", kind="reasoning", item_id="hidden-1"),
                            item("Partial <answer>"),
                            *tail,
                        ]
                    ),
                    initialized=True,
                )
                with self.assertRaises(RpcError) as caught:
                    client.wait_for_turn("turn-1")
                self.assertEqual(
                    getattr(caught.exception, "partial_text", None), "Partial <answer>"
                )

    def test_worker_persists_partial_notice_without_success_or_reinvocation(self) -> None:
        class Client(WorkerClient):
            def wait_for_turn(self, _turn_id):
                cast(Any, self).on_visible_item("answer-1", "Partial <answer>", "unknown")
                raise CodexTurnError(
                    RpcError("HTTP 429 Too Many Requests; private diagnostic"),
                    "Partial <answer>",
                )

        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        client = Client()
        worker = fixture.worker(client)
        try:
            job_id = fixture.enqueue()
            worker.run_cycle()
            job = worker.state.get_provider_job(job_id)
            notice = worker.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(job.status, "indeterminate")
            self.assertIn("429", notice.telegram_html)
            self.assertIn("Partial &lt;answer&gt;", notice.telegram_html)
            self.assertNotIn("private diagnostic", notice.telegram_html)
            self.assertIn("incomplete", notice.telegram_html.lower())
            self.assertIn("What happened:", notice.telegram_html)
            self.assertIn("Saved:", notice.telegram_html)
            self.assertIn("Next:", notice.telegram_html)
            self.assertEqual(worker.state.telegram_contract_version(job.session_id), 0)
            lease = worker.state.lease_telegram_outbox("codex", "fictional-sender")
            assert lease is not None and lease.lease_token is not None
            worker.state.mark_telegram_outbox_delivered(
                lease.outbox_id,
                lease.lease_token,
                telegram_message_id=1,
            )
            self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
            reliability = worker.state.reliability_snapshot()
            self.assertEqual(reliability["partial_outcomes"], 1)
            self.assertEqual(reliability["uncertain_execution"], 1)
            self.assertTrue(worker.run_cycle())  # one bounded, read-only observation
            self.assertFalse(worker.run_cycle())
            self.assertEqual(client.turns, 1)
        finally:
            worker.close()
            fixture.tearDown()

    def test_embedded_handled_disconnect_recovers_without_reinvocation(self) -> None:
        class Client(embedded_fixtures.QueueClient):
            def __init__(self) -> None:
                super().__init__()
                self.reads = 0

            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise RpcError("fictional embedded transport disconnect")

            def read_completed_turn(self, **_kwargs: object) -> TurnResult:
                self.reads += 1
                return TurnResult("Embedded recovered after disconnect", None, None)

        fixture = embedded_fixtures.EmbeddedQueueServiceTests()
        fixture.setUp()
        client = Client()
        service, telegram = fixture.service(client)
        try:
            service.handle_update(embedded_fixtures.update(1, "Fictional task"))
            self.assertTrue(service.run_embedded_queue_cycle())
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            job = service.state.provider_jobs_for_topic(topic.topic_id)[0]
            self.assertEqual(job.status, "completed")
            self.assertEqual(len(client.turn_threads), 1)
            self.assertEqual(client.reads, 1)
            self.assertTrue(
                any("Embedded recovered after disconnect" in text for text in telegram.sent)
            )
        finally:
            service.close()
            fixture.tearDown()

    def test_handled_disconnect_recovers_exact_completed_turn_without_reinvocation(self) -> None:
        class Client(WorkerClient):
            def __init__(self) -> None:
                super().__init__()
                self.reads = 0

            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise RpcError("fictional transport disconnect")

            def read_completed_turn(self, **_kwargs: object) -> TurnResult:
                self.reads += 1
                return TurnResult("Recovered after disconnect", None, None)

        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        client = Client()
        worker = fixture.worker(client)
        try:
            job_id = fixture.enqueue()
            worker.run_cycle()
            job = worker.state.get_provider_job(job_id)
            outbox = worker.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(job.status, "result_ready")
            self.assertIn("Recovered after disconnect", outbox.telegram_html)
            self.assertEqual(client.turns, 1)
            self.assertEqual(client.reads, 1)
            self.assertEqual(worker.state.reliability_snapshot()["recovered_results"], 1)
        finally:
            worker.close()
            fixture.tearDown()

    def test_reliability_snapshot_reports_outcomes_and_queue_ages(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        client = WorkerClient()
        worker = fixture.worker(client)
        try:
            completed_id = fixture.enqueue(1)
            worker.run_cycle()
            outbox = worker.state.lease_telegram_outbox("codex", "sender")
            assert outbox is not None and outbox.lease_token is not None
            worker.state.mark_telegram_outbox_delivered(
                outbox.outbox_id, outbox.lease_token, telegram_message_id=1
            )
            fixture.enqueue(2)

            snapshot = cast(dict[str, int | None], worker.state.status_snapshot()["reliability"])
            self.assertEqual(snapshot["accepted_requests"], 2)
            self.assertEqual(snapshot["delivered_final_results"], 1)
            self.assertEqual(snapshot["queued_work"], 1)
            self.assertEqual(snapshot["pending_delivery"], 0)
            self.assertEqual(snapshot["uncertain_execution"], 0)
            self.assertEqual(snapshot["partial_outcomes"], 0)
            self.assertIsInstance(snapshot["oldest_queue_age_seconds"], int)
            self.assertIsInstance(snapshot["last_delivery_delay_seconds"], int)
            self.assertEqual(worker.state.get_provider_job(completed_id).status, "completed")
        finally:
            worker.close()
            fixture.tearDown()

    def test_embedded_worker_delivers_partial_failure_without_success(self) -> None:
        class Client(embedded_fixtures.QueueClient):
            def wait_for_turn(self, _turn_id):
                raise CodexTurnError(RpcError("HTTP 429 private diagnostic"), "Partial <answer>")

        fixture = embedded_fixtures.EmbeddedQueueServiceTests()
        fixture.setUp()
        client = Client()
        service, telegram = fixture.service(client)
        try:
            service.handle_update(embedded_fixtures.update(1, "Fictional task"))
            service.run_embedded_queue_cycle()
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            job = service.state.provider_jobs_for_topic(topic.topic_id)[0]
            self.assertEqual(job.status, "indeterminate")
            self.assertTrue(any("Partial &lt;answer&gt;" in text for text in telegram.sent))
            self.assertEqual(len(client.turn_threads), 1)
        finally:
            service.close()
            fixture.tearDown()

    def test_compatibility_sender_persists_server_retry_minimum(self) -> None:
        fixture = sender_fixtures.TelegramOutboxSenderTests()
        fixture.setUp()
        state = HubState.open(fixture.config.state_path)
        service = cast(
            Any, embedded_fixtures.ProjectHubService.__new__(embedded_fixtures.ProjectHubService)
        )
        service.config = fixture.config
        bot = Bot()
        service._provider_telegram = lambda _agent_id: bot
        try:
            job_id = fixture.ready_outbox("opencode", 1)
            before = datetime.now(timezone.utc)
            with patch.object(
                bot,
                "send_html",
                side_effect=TelegramError("Fictional cooldown", retry_after=60, status_code=429),
            ):
                service._deliver_embedded_outbox(state, "opencode")
            notice = state.get_telegram_outbox_for_job(job_id)
            self.assertGreaterEqual(
                datetime.fromisoformat(notice.available_at), before + timedelta(seconds=60)
            )
        finally:
            state.close()
            fixture.tearDown()

    def test_partial_notice_is_bounded_escaped_and_marks_omission(self) -> None:
        error = CodexTurnError(RpcError("Private error detail"), "x" * 30_000 + "<latest>")
        self.assertLessEqual(len(error.partial_text), MAX_PARTIAL_TEXT)
        notice = codex_failure_notice(error)
        self.assertIn("Earlier partial text omitted", notice)
        self.assertIn("&lt;latest&gt;", notice)
        self.assertNotIn("Private error detail", notice)
        self.assertIn("wait for read-only reconciliation", notice)
        self.assertIn("do not repeat the task", notice)

    def test_retry_backoff_respects_hint_and_caps_without_overflow(self) -> None:
        self.assertEqual(delivery_retry_delay(RuntimeError(), 1), 1)
        self.assertEqual(delivery_retry_delay(RuntimeError(), 2), 2)
        self.assertEqual(delivery_retry_delay(RuntimeError(), 10_000), 300)
        self.assertEqual(delivery_retry_delay(TelegramError("x", retry_after=86400), 2), 86400)
        self.assertEqual(delivery_retry_delay(TelegramError("x", retry_after=-1), 2), 2)

    def test_preparation_failure_does_not_claim_provider_may_have_run(self) -> None:
        class Client(WorkerClient):
            def start_thread(self, **kwargs):
                raise RpcError("Fictional setup failure")

        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        client = Client()
        worker = fixture.worker(client)
        try:
            job_id = fixture.enqueue()
            worker.run_cycle()
            self.assertEqual(client.turns, 0)
            self.assertEqual(worker.state.get_provider_job(job_id).status, "failed")
            notice = worker.state.get_telegram_outbox_for_job(job_id)
            self.assertIn("before starting", notice.telegram_html)
        finally:
            worker.close()
            fixture.tearDown()

    def test_telegram_cooldown_survives_sender_restart_then_delivers(self) -> None:
        class LimitedBot(Bot):
            def send_html(
                self,
                chat_id,
                thread_id,
                html,
                *,
                reply_markup=None,
                reply_to_message_id=None,
            ):
                self.sent.append((chat_id, thread_id, html))
                raise TelegramError(
                    "Fictional rate limit",
                    operation="send_message",
                    status_code=429,
                    retry_after=60,
                )

        fixture = sender_fixtures.TelegramOutboxSenderTests()
        fixture.setUp()
        sender = fixture.sender(opencode=LimitedBot(), antigravity=Bot())
        try:
            job_id = fixture.ready_outbox("opencode", 1)
            now = datetime.now(timezone.utc)
            sender._deliver_one("opencode", now=now)
            sender.close()
            recovered = Bot()
            sender = fixture.sender(opencode=recovered, antigravity=Bot())
            self.assertFalse(sender._deliver_one("opencode", now=now + timedelta(seconds=59)))
            self.assertTrue(sender._deliver_one("opencode", now=now + timedelta(seconds=61)))
            self.assertEqual(len(recovered.sent), 1)
            self.assertEqual(sender.state.get_provider_job(job_id).status, "completed")
        finally:
            sender.close()
            fixture.tearDown()
