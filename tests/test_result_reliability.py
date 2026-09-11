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
            def send_html(self, chat_id, thread_id, html):
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
