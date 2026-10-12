from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.catalog_refresh import refresh_provider_catalogs
from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError
from hermes_codex_router.codex_result_lifecycle import retire_completed_connection
from hermes_codex_router.provider_catalog import ProviderModel
from hermes_codex_router.state import StateError
from tests import test_codex_worker as external
from tests import test_embedded_queue_service as embedded
from tests.delivery_fixture import complete_final_delivery


class ResultLifecycleTests(unittest.TestCase):
    def external_worker(self, *, close_error=False):
        fixture = external.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        statuses = []
        consumes = []
        resumes = []
        job_id = fixture.enqueue()

        class Client(external.WorkerClient):
            def consume_completed_connection(self, *, thread_id, turn_id):
                consumes.append((thread_id, turn_id))
                return True

            def close(self):
                statuses.append(worker.state.get_provider_job(job_id).status)
                if close_error:
                    raise OSError("fictional private transport detail")

            def resume_thread(self, **kwargs):
                resumes.append(kwargs["thread_id"])
                return super().resume_thread(**kwargs)

        clients = [Client(), Client()]
        clients[1].turn_id_offset = 1

        class Supervisor(external.WorkerSupervisor):
            calls = 0

            def client(self, *, allow_fallback=True, deadline=None):
                client = clients[self.calls]
                self.calls += 1
                return client

        supervisor = Supervisor(clients[0])
        worker = external.CodexQueueWorker(
            fixture.config, registry=fixture.registry, supervisor=cast(Any, supervisor)
        )
        self.addCleanup(worker.close)
        return fixture, worker, supervisor, clients, job_id, statuses, consumes, resumes

    def test_external_result_precedes_retirement_and_next_client_resumes_saved_thread(self):
        _, worker, supervisor, clients, job_id, statuses, consumes, resumes = self.external_worker()
        self.assertTrue(worker.run_cycle())
        self.assertEqual(statuses, ["result_ready"])
        self.assertEqual(consumes, [("thread-1", "turn-1")])
        self.assertIsNone(worker._codex_client)
        old = worker.state.get_provider_job(job_id)
        delivery = worker.state.lease_telegram_outbox("codex", "example-sender")
        assert delivery is not None and delivery.lease_token is not None
        complete_final_delivery(
            worker.state, delivery.outbox_id, delivery.lease_token, telegram_message_id=101
        )
        session = worker.state.get_session(old.session_id)
        child, _ = worker.state.enqueue_provider_job(
            idempotency_key="example:next",
            chat_id=old.chat_id,
            message_id=2,
            topic_id=old.topic_id,
            agent_id="codex",
            session_id=old.session_id,
            session_generation=old.session_generation,
            provider_session_id=session.provider_session_id,
            model=old.model,
            effort=old.effort,
            payload_text="Example next task",
        )
        self.assertTrue(worker.run_cycle())
        self.assertEqual(supervisor.calls, 2)
        self.assertEqual(resumes, ["thread-1"])
        self.assertEqual([client.turns for client in clients], [1, 1])
        self.assertEqual(worker.state.get_provider_job(child.job_id).status, "result_ready")
        self.assertIsNotNone(worker.state.get_telegram_outbox_for_job(job_id))
        self.assertIsNotNone(worker.state.get_telegram_outbox_for_job(child.job_id))

    def test_close_failure_keeps_result_outbox_and_detached_cache_with_safe_warning(self):
        _, worker, _, _, job_id, statuses, _, _ = self.external_worker(close_error=True)
        self.assertTrue(worker.run_cycle())
        self.assertEqual(statuses, ["result_ready"])
        self.assertIsNone(worker._codex_client)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertIsNotNone(worker.state.get_telegram_outbox_for_job(job_id))
        event = worker.state.latest_runtime_event("codex", "completed_socket_retirement_error")
        assert event is not None
        self.assertNotIn("fictional private transport detail", str(event["detail"]))

    def test_failed_publication_does_not_consume_success_retirement_authority(self):
        _, worker, _, _, _, _, consumes, _ = self.external_worker()
        with patch(
            "hermes_codex_router.external_worker.PreparedResultPublisher.publish",
            side_effect=StateError("Example publication fault"),
        ):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(consumes, [])

    def test_covering_stop_wins_publication_without_success_retirement(self):
        _, worker, _, _, job_id, _, consumes, _ = self.external_worker()
        from hermes_codex_router.controller_result_publication import PreparedResultPublisher

        publish = PreparedResultPublisher.publish

        def stop_then_publish(publisher, publication):
            worker.state.request_emergency_stop(
                topic_id=publication.job.topic_id,
                chat_id=publication.job.chat_id,
                message_id=99,
                target_agent_id="codex",
            )
            return publish(publisher, publication)

        with patch.object(PreparedResultPublisher, "publish", stop_then_publish):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(consumes, [])
        self.assertEqual(worker.state.get_provider_job(job_id).status, "cancelled")

    def test_retirement_of_expected_client_preserves_newer_cached_client(self):
        _, worker, _, clients, job_id, statuses, _, _ = self.external_worker()
        from hermes_codex_router.controller_result_publication import PreparedResultPublisher

        publish = PreparedResultPublisher.publish

        def publish_then_replace(publisher, *args, **kwargs):
            result = publish(publisher, *args, **kwargs)
            worker._codex_client = cast(CodexAppServerClient, clients[1])
            return result

        with patch.object(PreparedResultPublisher, "publish", publish_then_replace):
            self.assertTrue(worker.run_cycle())
        self.assertIs(worker._codex_client, clients[1])
        self.assertEqual(statuses, ["result_ready"])
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")

    def embedded_service(self, *, inline=False, fail_limits=False):
        fixture = embedded.EmbeddedQueueServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        observed = []

        class Client(embedded.QueueClient):
            def consume_completed_connection(self, *, thread_id, turn_id):
                if inline:
                    statuses = service.state._connection.execute(
                        "SELECT status FROM turn_dispatches"
                    ).fetchall()
                    observed.append((thread_id, turn_id, tuple(row[0] for row in statuses)))
                else:
                    jobs = service.state.provider_jobs_for_topic(topic_id)
                    observed.append((thread_id, turn_id, tuple(job.status for job in jobs)))
                return True

        client = Client(fail_limits=fail_limits)
        service, telegram = fixture.service(client)
        if inline:
            service.config = replace(service.config, dispatch_mode="inline")
        self.addCleanup(service.close)
        self.assertTrue(service.handle_update(embedded.update(1, "Example task")))
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        topic_id = topic.topic_id
        return service, client, telegram, observed, topic_id

    def test_embedded_result_is_saved_before_retirement(self):
        service, _, _, observed, _ = self.embedded_service()
        self.assertTrue(service.run_embedded_queue_cycle())
        self.assertEqual(observed, [("thread-1", "turn-1", ("result_ready",))])
        self.assertIsNone(service._codex_client)

    def test_inline_dispatch_and_exact_first_thread_are_saved_before_retirement(self):
        service, _, telegram, observed, _ = self.embedded_service(inline=True, fail_limits=True)
        self.assertEqual(observed, [("thread-1", "turn-1", ("completed",))])
        self.assertIsNone(service._codex_client)
        row = service.state._connection.execute(
            "SELECT provider_session_id,response_excerpt FROM external_turn_excerpts"
        ).fetchone()
        self.assertEqual(tuple(row), ("thread-1", "Visible answer"))
        self.assertTrue(any("Visible answer" in text for text in telegram.sent))

    def test_queued_stale_and_explicit_catalog_refresh_never_read_productive_client(self):
        fixture = embedded.EmbeddedQueueServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        client = embedded.QueueClient(block=True)
        service, _ = fixture.service(client)
        self.addCleanup(service.close)
        service._catalog_cache().store(
            "codex",
            (ProviderModel("gpt-5.6-sol", "Example model", ("high",)),),
            source_version="codex model/list openai-only",
            observed_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        self.assertTrue(service.handle_update(embedded.update(1, "Example long task")))
        runner = threading.Thread(target=service.run_embedded_queue_cycle)
        runner.start()
        self.addCleanup(runner.join, 4)
        self.addCleanup(client.release.set)
        self.assertTrue(client.entered.wait(2))
        with patch.object(
            service, "_discover_provider_models", side_effect=AssertionError("no RPC")
        ):
            for refresh in (False, True):
                self.assertEqual(
                    service._provider_catalog("codex", refresh=refresh).models[0].model_id,
                    "gpt-5.6-sol",
                )
        self.assertIs(service._codex_client, client)
        self.assertTrue(runner.is_alive())
        client.release.set()
        runner.join(4)
        self.assertFalse(runner.is_alive())

    def test_warning_failure_never_reclassifies_success_or_exposes_exception_text(self):
        class Client:
            def consume_completed_connection(self, **kwargs):
                return True

        with self.assertLogs("hermes_codex_router", level="WARNING") as logs:
            retire_completed_connection(
                cast(CodexAppServerClient, Client()),
                thread_id="example-thread",
                turn_id="example-turn",
                retire=lambda: (_ for _ in ()).throw(RpcError("fictional private detail")),
                warning=lambda *_: (_ for _ in ()).throw(StateError("fictional private report")),
            )
        self.assertEqual(len(logs.output), 2)
        self.assertIn("codex_result_lifecycle.retirement", logs.output[0])
        self.assertIn("codex_result_lifecycle.warning", logs.output[1])
        self.assertNotIn("fictional private", " ".join(logs.output))

    def test_queued_other_catalogs_request_refresh_and_monitor_updates_them(self):
        # Claude projects configured choices locally; native-discovery runtimes
        # retain monitor-owned refresh without touching the productive client.
        for runtime in ("opencode", "antigravity"):
            with self.subTest(runtime=runtime):
                fixture = embedded.EmbeddedQueueServiceTests()
                fixture.setUp()
                try:
                    service, _ = fixture.service(embedded.QueueClient())
                    agent = replace(
                        service.config.agents[0],
                        agent_id=runtime,
                        runtime=runtime,
                        default_model="example-new",
                        default_effort="high",
                    )
                    service.config = replace(service.config, agents=(agent,))
                    cache = service._catalog_cache()
                    old = (ProviderModel("example-old", "Old", ("high",)),)
                    prior = cache.store(runtime, old, source_version="example-cli")
                    new = (ProviderModel("example-new", "New", ("high",)),)
                    with patch.object(service, "_discover_provider_models") as discovery:
                        self.assertEqual(
                            service._provider_catalog(runtime, refresh=True).models, prior.models
                        )
                        discovery.assert_not_called()
                    self.assertTrue(cache.is_stale(runtime))
                    with (
                        patch(
                            "hermes_codex_router.catalog_refresh.opencode_models", return_value=new
                        ),
                        patch(
                            "hermes_codex_router.catalog_refresh.antigravity_models",
                            return_value=new,
                        ),
                        patch(
                            "hermes_codex_router.catalog_refresh.provider_source_version",
                            return_value="example-cli",
                        ),
                    ):
                        result = refresh_provider_catalogs(service.config)
                    self.assertEqual(result.refreshed, (runtime,))
                    self.assertFalse(cache.is_stale(runtime))
                    self.assertEqual(
                        service._provider_catalog(runtime).models[0].model_id, "example-new"
                    )
                    service.close()
                finally:
                    fixture.tearDown()

    def test_poller_foreground_error_cannot_discard_active_embedded_client(self):
        fixture = embedded.EmbeddedQueueServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        client = embedded.QueueClient(block=True)
        service, telegram = fixture.service(client)
        self.addCleanup(service.close)
        self.assertTrue(service.handle_update(embedded.update(1, "Example long task")))
        runner = threading.Thread(target=service.run_embedded_queue_cycle)
        runner.start()
        self.assertTrue(client.entered.wait(2))
        stop = service._stop = threading.Event()

        def fail_foreground(_update):
            stop.set()
            raise ValueError("fictional foreground fault")

        try:
            with (
                patch.object(
                    telegram, "updates", return_value=[embedded.update(2, "/status")], create=True
                ),
                patch.object(service, "handle_update", side_effect=fail_foreground),
                patch.object(service, "_start_embedded_queue_consumer"),
                patch.object(service, "_start_controller_outbox_delivery"),
                patch.object(service, "_publish_runtime_health"),
                patch.object(service, "_record_telegram_poll_success"),
                patch.object(client, "close") as close,
            ):
                service.run_forever()
                close.assert_not_called()
            self.assertIs(service._codex_client, client)
        finally:
            client.release.set()
            runner.join(4)
        self.assertFalse(runner.is_alive())
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        saved = service.state.provider_jobs_for_topic(topic.topic_id)[0]
        self.assertIn(saved.status, {"result_ready", "completed"})
        self.assertIsNotNone(service.state.get_telegram_outbox_for_job(saved.job_id))
