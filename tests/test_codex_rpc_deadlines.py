from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import codex_appserver as protocol
from hermes_codex_router import codex_permissions as permissions
from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError
from hermes_codex_router.codex_permissions import CodexPermissionProfileError
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.root_blockers import persistent_root_blocker
from tests import test_codex_worker as external
from tests import test_embedded_queue_service as embedded
from tests.test_codex_appserver import FakeTransport


class Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now


def foreign_notification() -> dict:
    return {
        "method": "thread/status/changed",
        "params": {"threadId": "example-unrelated-thread"},
    }


def approval_notification() -> dict:
    return {
        "method": "item/commandExecution/requestApproval",
        "id": 71,
        "params": {
            "threadId": "example-thread",
            "turnId": "example-unconfirmed-turn",
            "itemId": "example-item",
            "command": "private fictional payload",
        },
    }


class FloodTransport(FakeTransport):
    def __init__(self, clock: Clock, *, step: float = 0.1):
        super().__init__([])
        self.clock = clock
        self.step = step

    def receive(self, *, timeout: float | None = None) -> dict:
        self.receive_timeouts.append(timeout)
        self.clock.now += self.step
        if self.clock.now > 400:
            raise AssertionError("unrelated frames renewed an unbounded RPC wait")
        return foreign_notification()


class SubmissionTransport(FloodTransport):
    def __init__(self, clock: Clock, root: Path):
        super().__init__(clock, step=5)
        self.root = root
        self.approval_sent = False

    def receive(self, *, timeout: float | None = None) -> dict:
        request = self.sent[-1]
        if request["method"] == "thread/start":
            self.receive_timeouts.append(timeout)
            return {
                "id": request["id"],
                "result": {
                    "thread": {"id": "example-thread"},
                    "cwd": str(self.root),
                    "model": "gpt-5.6-sol",
                    "modelProvider": "openai",
                    "approvalPolicy": "on-request",
                    "sandbox": "workspace-write",
                },
            }
        if request["method"] != "turn/start":
            raise AssertionError("unexpected RPC or approval answer")
        if not self.approval_sent:
            self.approval_sent = True
            self.receive_timeouts.append(timeout)
            self.clock.now += 5
            return approval_notification()
        return super().receive(timeout=timeout)


class RpcDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.clock = Clock()
        # Replace the protocol's module reference, never the process-wide clock
        # used by worker lease/heartbeat threads.
        self.enterContext(
            patch.object(protocol, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        )
        self.enterContext(
            patch.object(permissions, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        )

    def test_default_public_rpcs_expire_despite_more_than_1024_foreign_frames(self):
        for operation in ("initialize", "start_thread", "resume_thread", "read_rate_limits"):
            with self.subTest(operation=operation):
                self.clock.now = 0
                transport = FloodTransport(self.clock)
                client = CodexAppServerClient(transport, initialized=operation != "initialize")
                with self.assertRaisesRegex(RpcError, "deadline exceeded"):
                    if operation == "initialize":
                        client.initialize()
                    elif operation == "start_thread":
                        client.start_thread(
                            cwd=self.root, model="example-model", project_id="example-project"
                        )
                    elif operation == "resume_thread":
                        client.resume_thread(
                            thread_id="example-thread", cwd=self.root, model="example-model"
                        )
                    else:
                        client.read_rate_limits()
                self.assertGreater(len(transport.receive_timeouts), 1024)
                self.assertLessEqual(self.clock.now, 120.11)
                self.assertEqual(len(transport.sent), 1)
                self.assertTrue(
                    all(
                        value is not None and 0 < value <= 20
                        for value in transport.receive_timeouts
                    )
                )
                self.assertFalse(client.notifications)

    def test_progress_beyond_twenty_seconds_can_still_receive_a_response(self):
        clock = self.clock

        class Transport(FakeTransport):
            def receive(self, *, timeout=None):
                clock.now += 10
                return super().receive(timeout=timeout)

        transport = Transport(
            [foreign_notification(), foreign_notification(), {"id": 1, "result": {}}]
        )
        client = CodexAppServerClient(transport)
        client.initialize()
        self.assertTrue(client._initialized)
        self.assertEqual(clock.now, 30)
        self.assertTrue(
            all(value is not None and value <= 20 for value in transport.receive_timeouts)
        )

    def test_explicit_deadline_is_not_replaced_or_quiet_capped(self):
        transport = FakeTransport([{"id": 1, "result": {}}])
        client = CodexAppServerClient(transport)
        client.initialize(deadline=40)
        self.assertEqual(transport.receive_timeouts, [40])
        expired = FakeTransport([])
        with self.assertRaisesRegex(RpcError, "deadline exceeded"):
            CodexAppServerClient(expired).initialize(deadline=0)
        self.assertEqual(expired.sent, [])

    def test_matching_response_at_or_after_deadline_is_rejected(self):
        for explicit, arrival in ((None, 120), (None, 121), (4, 4), (4, 5)):
            with self.subTest(explicit=explicit, arrival=arrival):
                self.clock.now = 0
                clock = self.clock

                class Transport(FakeTransport):
                    def receive(self, *, timeout=None):
                        clock.now = arrival
                        return super().receive(timeout=timeout)

                transport = Transport([{"id": 1, "result": {}}])
                client = CodexAppServerClient(transport)
                with self.assertRaisesRegex(RpcError, "deadline exceeded"):
                    client.initialize(deadline=explicit)
                self.assertFalse(client._initialized)
                self.assertEqual(len(transport.sent), 1)

    def test_late_rejection_and_approval_are_not_processed(self):
        for message in (
            {"id": 1, "error": {"message": "fictional late rejection"}},
            {
                "method": "item/commandExecution/requestApproval",
                "id": 71,
                "params": {
                    "threadId": "example-thread",
                    "turnId": "example-turn",
                    "itemId": "example-item",
                },
            },
        ):
            with self.subTest(message=message):
                self.clock.now = 0
                clock = self.clock

                class Transport(FakeTransport):
                    def receive(self, *, timeout=None):
                        clock.now = 300
                        return super().receive(timeout=timeout)

                transport = Transport([message])
                client = CodexAppServerClient(transport, initialized=True)
                early = []
                client.on_preacceptance_approval = early.append
                with self.assertRaisesRegex(RpcError, "deadline exceeded"):
                    client.start_turn(
                        thread_id="example-thread",
                        cwd=self.root,
                        text="original task",
                        model="example-model",
                        effort="high",
                    )
                self.assertEqual(early, [])
                self.assertEqual(len(transport.sent), 1)

    def test_time_spent_sending_consumes_the_original_default_budget(self):
        clock = self.clock

        class Transport(FakeTransport):
            def send(self, message):
                super().send(message)
                clock.now = 120

        transport = Transport([])
        with self.assertRaisesRegex(RpcError, "deadline exceeded"):
            CodexAppServerClient(transport).initialize()
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.receive_timeouts, [])

    def test_managed_metadata_timeout_clears_preparation_without_legacy_fallback(self):
        transport = FloodTransport(self.clock, step=1)
        client = CodexAppServerClient(
            transport, initialized=True, permission_profile="example-project-policy"
        )
        with self.assertRaises(CodexPermissionProfileError):
            client.start_thread(cwd=self.root, model="example-model", project_id="example-project")
        self.assertEqual(self.clock.now, 10)
        self.assertIsNone(client._permission_binding)
        self.assertFalse(client._permission_preparing)
        self.assertEqual(client._preparation_settings, [])
        self.assertNotIn("turn/start", [message["method"] for message in transport.sent])

    def test_early_approval_keeps_full_submission_window_without_answer_or_replay(self):
        transport = SubmissionTransport(self.clock, self.root)
        client = CodexAppServerClient(transport, initialized=True)
        early = []
        client.on_preacceptance_approval = early.append
        with self.assertRaisesRegex(RpcError, "deadline exceeded"):
            client.start_turn(
                thread_id="example-thread",
                cwd=self.root,
                text="approved original task",
                model="example-model",
                effort="high",
            )
        self.assertEqual(transport.receive_timeouts[0], 300)
        self.assertEqual(self.clock.now, 300)
        self.assertEqual([event.kind for event in early], ["approval_requested"])
        self.assertNotIn("private", repr(early))
        self.assertEqual([message["method"] for message in transport.sent], ["turn/start"])
        self.assertIsNone(client._activity_turn_id)
        self.assertFalse(client._collecting_rate_limits)

    def test_quiet_early_approval_can_accept_submission_later_in_the_window(self):
        clock = self.clock

        class Transport(FakeTransport):
            def receive(self, *, timeout=None):
                clock.now = 45 if not self.receive_timeouts else 250
                return super().receive(timeout=timeout)

        scripted = Transport(
            [
                approval_notification(),
                {"id": 1, "result": {"turn": {"id": "example-unconfirmed-turn"}}},
            ]
        )
        client = CodexAppServerClient(scripted, initialized=True)
        early = []
        client.on_preacceptance_approval = early.append
        accepted = client.start_turn(
            thread_id="example-thread",
            cwd=self.root,
            text="original task",
            model="example-model",
            effort="high",
        )
        self.assertEqual(accepted, "example-unconfirmed-turn")
        self.assertEqual(scripted.receive_timeouts, [300, 255])
        self.assertEqual([event.kind for event in early], ["approval_requested"])
        self.assertEqual([message["method"] for message in scripted.sent], ["turn/start"])

    def test_external_submission_timeout_preserves_uncertainty_and_thread_checkpoint(self):
        fixture = external.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = fixture.registry.require_project("example-project").root
        transport = SubmissionTransport(self.clock, root)
        client = CodexAppServerClient(transport, initialized=True)
        job_id = fixture.enqueue(payload="approved original task")
        worker = external.CodexQueueWorker(
            fixture.config,
            registry=fixture.registry,
            supervisor=cast(Any, external.WorkerSupervisor(cast(Any, client))),
        )
        self.addCleanup(worker.close)
        self.assertTrue(worker.run_cycle())
        self.assert_unknown_submission(worker.state, job_id, transport)
        self.assertFalse(worker.run_cycle())

    def test_embedded_submission_timeout_preserves_uncertainty_and_thread_checkpoint(self):
        fixture = embedded.EmbeddedQueueServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = fixture.registry.require_project("example-project").root
        transport = SubmissionTransport(self.clock, root)
        client = CodexAppServerClient(transport, initialized=True)
        service, _ = fixture.service(cast(Any, client))
        self.addCleanup(service.close)
        self.assertTrue(service.handle_update(embedded.update(1, "approved original task")))
        self.assertTrue(service.run_embedded_queue_cycle())
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        job = service.state.provider_jobs_for_topic(topic.topic_id)[0]
        self.assert_unknown_submission(service.state, job.job_id, transport)
        self.assertFalse(service.run_embedded_queue_cycle())

    def assert_unknown_submission(self, state, job_id, transport):
        job = state.get_provider_job(job_id)
        self.assertEqual((job.status, job.error_class), ("indeterminate", "ambiguous_execution"))
        self.assertEqual(job.payload_text, "approved original task")
        checkpoint = ExecutionJournal(state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["provider_thread_id"], "example-thread")
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertEqual(self.clock.now, 300)
        with state._immediate_transaction():
            blocker = persistent_root_blocker(state._connection, topic_id=job.topic_id)
        assert blocker is not None
        self.assertEqual((blocker.kind, blocker.cause_job_id), ("uncertain", job_id))
        self.assertEqual(
            [message["method"] for message in transport.sent], ["thread/start", "turn/start"]
        )
        self.assertIsNotNone(state.get_telegram_outbox_for_job(job_id))


if __name__ == "__main__":
    unittest.main()
