from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.root_blockers import persistent_root_blocker
from tests import test_claude_native_worker as fixtures


class ClaudePostCompletionFaultTests(unittest.TestCase):
    def seed(
        self,
    ) -> tuple[
        fixtures.ClaudeNativeWorkerTests,
        str,
        fixtures.ObservingClaudeAdapter,
        ExternalQueueWorker,
    ]:
        fixture = fixtures.ClaudeNativeWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        job_id = fixture.enqueue(1)
        adapter = fixtures.ObservingClaudeAdapter(fixture.path)
        return fixture, job_id, adapter, fixture.worker(adapter)

    def test_completed_checkpoint_is_recovered_after_prepublication_fault(self) -> None:
        fixture, job_id, adapter, worker = self.seed()
        with patch(
            "hermes_codex_router.external_worker.prepare_worker_artifacts",
            side_effect=OSError("fictional artifact fault"),
        ):
            self.assertTrue(worker.run_cycle())
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Fictional visible answer")
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(
            worker.state.get_provider_result(job_id).visible_response, "Fictional visible answer"
        )
        self.assertIsNone(
            persistent_root_blocker(
                worker.state._connection, topic_id=worker.state.get_provider_job(job_id).topic_id
            )
        )
        self.assertEqual(len(adapter.calls), 1)
        self.assertFalse(worker.run_cycle())
        self.assertEqual(len(adapter.calls), 1)

    def test_persistent_result_commit_fault_keeps_saved_completion_and_waits_without_replay(
        self,
    ) -> None:
        fixture, job_id, adapter, worker = self.seed()
        with patch.object(
            worker.state,
            "commit_provider_result",
            side_effect=OSError("fictional persistent commit fault"),
        ):
            self.assertTrue(worker.run_cycle())
            self.assertFalse(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(job_id).status, "executing")
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Fictional visible answer")
        worker.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=78,
            title="Fictional second topic",
            execution_root=fixture.root,
        )
        fixture.fixture.enqueue("claude", 2, thread_id=78)
        self.assertIsNone(
            worker.state.lease_provider_job(
                "claude",
                "fictional-second-worker",
                max_parallel_roots=2,
                agent_capacities={"claude": 2},
            )
        )
        self.assertEqual(len(adapter.calls), 1)
        current = worker.state.get_provider_job(job_id)
        assert current.lease_token is not None
        worker.state.heartbeat_provider_job(
            job_id,
            current.lease_token,
            lease_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        self.assertTrue(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(len(adapter.calls), 1)

    def test_completion_recovery_failure_does_not_release_stop_without_terminal_commit(
        self,
    ) -> None:
        fixture, job_id, adapter, worker = self.seed()
        original = worker.state.commit_provider_result
        called = False

        def commit_with_stop(*args, **kwargs):
            nonlocal called
            if not called:
                called = True
                job = worker.state.get_provider_job(job_id)
                worker.state.request_emergency_stop(
                    topic_id=job.topic_id,
                    chat_id=job.chat_id,
                    message_id=10,
                    target_agent_id="claude",
                )
                raise OSError("fictional first commit fault")
            return original(*args, **kwargs)

        with patch.object(worker.state, "commit_provider_result", side_effect=commit_with_stop):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(job_id).status, "cancelled")
        self.assertIsNone(
            persistent_root_blocker(
                worker.state._connection, topic_id=worker.state.get_provider_job(job_id).topic_id
            )
        )
        self.assertEqual(len(adapter.calls), 1)

    def test_binding_change_before_recovery_suppresses_completed_output(self) -> None:
        fixture, job_id, adapter, worker = self.seed()

        def artifact_fault(*args, **kwargs):
            job = worker.state.get_provider_job(job_id)
            with worker.state._connection:
                worker.state._connection.execute(
                    "UPDATE agent_sessions SET provider_session_id=? WHERE session_id=?",
                    (fixtures.OTHER_UUID, job.session_id),
                )
            raise OSError("fictional changed binding")

        with patch(
            "hermes_codex_router.external_worker.prepare_worker_artifacts",
            side_effect=artifact_fault,
        ):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        self.assertIsNotNone(
            persistent_root_blocker(
                worker.state._connection, topic_id=worker.state.get_provider_job(job_id).topic_id
            )
        )
        self.assertNotIn(
            "Fictional visible answer",
            worker.state.get_telegram_outbox_for_job(job_id).telegram_html,
        )
        self.assertEqual(len(adapter.calls), 1)
