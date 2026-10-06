"""Absolute RPC expiry grants saved retry only at the proven preparation boundary."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import codex_appserver as protocol
from hermes_codex_router.codex_appserver import CodexAppServerClient
from hermes_codex_router.codex_failure import CodexPreparationError
from hermes_codex_router.codex_retry_policy import preparation_retry_binding
from hermes_codex_router.codex_rpc import RpcError
from hermes_codex_router.preexecution_retry_state import PreexecutionRetryState
from tests import test_codex_worker as external
from tests import test_embedded_queue_service as embedded
from tests.test_codex_rpc_deadlines import Clock, FloodTransport, SubmissionTransport


class PreparationDeadlineRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.enterContext(
            patch.object(protocol, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        )

    def test_real_preparation_expiry_offers_bound_saved_payload_retry_in_both_workers(self) -> None:
        for mode in ("external", "embedded"):
            with self.subTest(mode=mode):
                self.clock.now = 0
                transport = FloodTransport(self.clock)
                client = CodexAppServerClient(transport, initialized=True)
                if mode == "external":
                    fixture = external.CodexQueueWorkerTests()
                    fixture.setUp()
                    self.addCleanup(fixture.tearDown)
                    root = fixture.registry.require_project("example-project").root
                    job_id = fixture.enqueue(payload="Run the six approved fictional scenarios.")
                    worker = external.CodexQueueWorker(
                        fixture.config,
                        registry=fixture.registry,
                        supervisor=cast(Any, external.WorkerSupervisor(cast(Any, client))),
                    )
                    self.addCleanup(worker.close)
                    self.assertTrue(worker.run_cycle())
                    self.assertFalse(worker.run_cycle())
                    state = worker.state
                else:
                    fixture = embedded.EmbeddedQueueServiceTests()
                    fixture.setUp()
                    self.addCleanup(fixture.tearDown)
                    root = fixture.registry.require_project("example-project").root
                    service, _ = fixture.service(cast(Any, client))
                    self.addCleanup(service.close)
                    self.assertTrue(
                        service.handle_update(
                            embedded.update(1, "Run the six approved fictional scenarios.")
                        )
                    )
                    self.assertTrue(service.run_embedded_queue_cycle())
                    topic = service.state.find_topic(-1001234567890, 77)
                    assert topic is not None
                    job_id = service.state.provider_jobs_for_topic(topic.topic_id)[0].job_id
                    state = service.state
                job = state.get_provider_job(job_id)
                self.assertEqual((job.status, job.error_class), ("failed", "pre_execution"))
                self.assertGreater(len(transport.receive_timeouts), 1024)
                self.assertEqual([entry["method"] for entry in transport.sent], ["thread/start"])
                notice = state.get_telegram_outbox_for_job(job_id)
                assert notice is not None
                self.assertIn("Reply exactly retry", notice.telegram_html)
                if notice.telegram_message_id is None:
                    delivery = state.lease_telegram_outbox("codex", "example-sender")
                    assert delivery is not None and delivery.lease_token is not None
                    state.mark_telegram_outbox_delivered(
                        delivery.outbox_id, delivery.lease_token, telegram_message_id=101
                    )
                    notice = state.get_telegram_outbox_for_job(job_id)
                    assert notice is not None
                topic = state.get_topic(job.topic_id)
                child, created = PreexecutionRetryState(state).retry_from_notice(
                    source_job_id=job_id,
                    chat_id=topic.chat_id,
                    thread_id=topic.thread_id,
                    notice_message_id=notice.telegram_message_id,
                    reply_message_id=31,
                    canonical_root=root,
                    model_provider=None,
                    provider_runtime="codex",
                )
                self.assertTrue(created)
                self.assertEqual(child.payload_text, job.payload_text)
                self.assertEqual(child.session_id, job.session_id)
                self.assertEqual(child.session_generation, job.session_generation)
                self.assertEqual([entry["method"] for entry in transport.sent], ["thread/start"])

    def test_submission_expiry_and_matching_error_text_cannot_grant_retry(self) -> None:
        fixture = external.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = fixture.registry.require_project("example-project").root
        client = CodexAppServerClient(SubmissionTransport(self.clock, root), initialized=True)
        with self.assertRaises(RpcError) as caught:
            client.start_turn(
                thread_id="example-thread",
                cwd=root,
                text="approved task",
                model="example-model",
                effort="high",
            )
        self.assertIsNone(
            preparation_retry_binding(caught.exception, root=root, model_provider=None)
        )
        generic = CodexPreparationError("Codex request deadline exceeded")
        generic.__cause__ = RpcError("Codex request deadline exceeded")
        self.assertIsNone(preparation_retry_binding(generic, root=root, model_provider=None))
