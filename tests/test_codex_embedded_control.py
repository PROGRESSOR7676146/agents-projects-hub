"""Embedded execution retains mandatory progress, stop and independent controls."""

from __future__ import annotations

import threading
import unittest
from contextlib import closing
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import TurnResult
from hermes_codex_router.codex_late_control import CodexControlMaintenance
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState
from tests import test_codex_worker as external
from tests import test_embedded_queue_service as embedded


class EmbeddedCodexControlTests(unittest.TestCase):
    def fixture(self):
        fixture = embedded.EmbeddedQueueServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_native_completed_with_covering_stop_commits_cancel_and_saved_text(self):
        fixture = self.fixture()

        class Client(embedded.QueueClient):
            def wait_for_turn(self, _turn_id):
                with closing(
                    HubState.open_existing(fixture.config.state_path, codex_permission_profile=None)
                ) as peer:
                    topic = peer.find_topic(-1001234567890, 77)
                    assert topic is not None
                    peer.request_emergency_stop(
                        topic_id=topic.topic_id,
                        chat_id=topic.chat_id,
                        message_id=99,
                        target_agent_id="codex",
                    )
                return TurnResult("Example withheld native final", None, None)

        service, _ = fixture.service(Client())
        self.addCleanup(service.close)
        self.assertTrue(service.handle_update(embedded.update(1, "Example task")))
        self.assertTrue(service.run_embedded_queue_cycle())
        job = service.state._connection.execute("SELECT * FROM provider_jobs").fetchone()
        assert job is not None
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job["lease_token"])
        saved = ExecutionJournal(service.state).read(job["job_id"])
        assert saved is not None
        self.assertEqual(saved["completed_text"], "Example withheld native final")
        self.assertIsNone(service.state.pending_emergency_stop_for_job(job["job_id"]))
        self.assertEqual(
            service.state._connection.execute(
                "SELECT COUNT(*) FROM provider_job_results"
            ).fetchone()[0],
            0,
        )

    def test_both_consumers_preserve_progress_enabled_execution_journal(self):
        for runtime in ("embedded", "external"):
            with self.subTest(runtime=runtime):
                if runtime == "embedded":
                    fixture = self.fixture()
                    fixture.config = replace(fixture.config, outbox_runtime="external")

                    class Client(embedded.QueueClient):
                        def wait_for_turn(self, _turn_id):
                            callback = getattr(self, "on_visible_item", None)
                            assert callback is not None
                            callback("example-progress", "Example visible progress", "commentary")
                            return TurnResult("Example final", None, None)

                    consumer, _ = fixture.service(Client())
                    self.addCleanup(consumer.close)
                    consumer.handle_update(embedded.update(1, "Example task"))
                    consumer.run_embedded_queue_cycle()
                else:
                    fixture = external.CodexQueueWorkerTests()
                    fixture.setUp()
                    self.addCleanup(fixture.tearDown)
                    fixture.config = replace(fixture.config, outbox_runtime="external")

                    class WorkerClient(external.WorkerClient):
                        def wait_for_turn(self, _turn_id):
                            callback = getattr(self, "on_visible_item", None)
                            assert callback is not None
                            callback("example-progress", "Example visible progress", "commentary")
                            return TurnResult("Example final", None, None)

                    fixture.enqueue()
                    consumer = fixture.worker(WorkerClient())
                    self.addCleanup(consumer.close)
                    consumer.run_cycle()
                rows = consumer.state._connection.execute(
                    "SELECT visible_text FROM provider_visible_items WHERE phase='commentary'"
                ).fetchall()
                self.assertEqual([row[0] for row in rows], ["Example visible progress"])
                delivery = consumer.state._connection.execute(
                    "SELECT sender_agent_id,telegram_html FROM provider_progress_deliveries"
                ).fetchall()
                self.assertEqual(len(delivery), 1)
                self.assertEqual(delivery[0][0], "codex")
                self.assertIn("Example visible progress", delivery[0][1])

    def test_embedded_maintenance_is_independent_and_forbids_fallback(self):
        fixture = self.fixture()
        service, _ = fixture.service(embedded.QueueClient())
        self.addCleanup(service.close)
        entered, release = threading.Event(), threading.Event()
        acquisitions = []

        class Supervisor(embedded.FakeSupervisor):
            def client(self, *, allow_fallback=True, deadline=None):
                acquisitions.append((allow_fallback, deadline))
                return super().client(allow_fallback=allow_fallback, deadline=deadline)

        service.supervisor = cast(Any, Supervisor(embedded.QueueClient()))

        def maintenance_run(maintenance):
            with closing(
                HubState.open_existing(maintenance.config.state_path, codex_permission_profile=None)
            ) as state:
                self.assertIsNot(state._connection, service.state._connection)
                maintenance.client_factory(123.0)
                entered.set()
                release.wait(2)

        with (
            patch.object(CodexControlMaintenance, "run_forever", maintenance_run),
            patch.object(service, "_embedded_queue_loop", return_value=None),
        ):
            service._start_embedded_queue_consumer()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(acquisitions, [(False, 123.0)])
                self.assertEqual(len(service._codex_maintenance), 1)
            finally:
                release.set()
                service.close()
        self.assertTrue(service._queue_stop.is_set())
        self.assertFalse(service._codex_maintenance[0][1].is_alive())


if __name__ == "__main__":
    unittest.main()
