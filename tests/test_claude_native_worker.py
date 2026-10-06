from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.claude_stream import ClaudeStreamError, ClaudeTerminalFailure
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_runtime import ExternalCliAdapter, ExternalTurnResult
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import HubState
from tests import test_external_worker as worker_fixtures
from tests.test_claude_cli_capabilities import HELP

NATIVE_UUID = "00000000-0000-4000-8000-000000000001"
OTHER_UUID = "019abcde-1234-7fff-8fff-0123456789ab"


class ObservingClaudeAdapter:
    runtime = "claude"

    def __init__(self, state_path: Path, *, outcome: str = "success", stop: bool = False):
        self.state_path = state_path
        self.outcome = outcome
        self.stop = stop
        self.calls: list[dict[str, Any]] = []
        self.observed: list[tuple[Any, ...]] = []

    def run_turn(
        self, *, session_id: str | None = None, new_session_id: str | None = None, **kwargs: Any
    ) -> ExternalTurnResult:
        self.calls.append(dict(session_id=session_id, new_session_id=new_session_id, **kwargs))
        # This connection is opened inside the invocation, before any provider result.
        with sqlite3.connect(self.state_path) as observer:
            observer.row_factory = sqlite3.Row
            job = observer.execute(
                "SELECT * FROM provider_jobs WHERE agent_id='claude' AND status='executing'"
            ).fetchone()
            assert job is not None
            current = observer.execute(
                "SELECT provider_session_id FROM agent_sessions WHERE session_id=?",
                (job["session_id"],),
            ).fetchone()[0]
            checkpoint = observer.execute(
                "SELECT provider_thread_id,project_root,provider_turn_id "
                "FROM provider_execution_checkpoints WHERE job_id=?",
                (job["job_id"],),
            ).fetchone()
            self.observed.append((current, tuple(checkpoint) if checkpoint is not None else None))
        expected = session_id or new_session_id or NATIVE_UUID
        if self.stop:
            state = HubState.open(self.state_path, codex_permission_profile=None)
            try:
                state.request_emergency_stop(
                    topic_id=job["topic_id"],
                    chat_id=job["chat_id"],
                    message_id=10,
                    target_agent_id="claude",
                )
            finally:
                state.close()
        if self.outcome == "quota":
            raise ClaudeTerminalFailure(
                "claude_quota_exhausted", "Terminal quota rejection; reset is unknown.", expected
            )
        if self.outcome == "mismatched_failure":
            wrong = OTHER_UUID if expected != OTHER_UUID else NATIVE_UUID
            raise ClaudeTerminalFailure(
                "claude_provider_failure", "Terminal provider failure.", wrong
            )
        if self.outcome == "contradictory":
            raise ClaudeStreamError("claude returned a conflicting terminal outcome")
        returned = (
            (OTHER_UUID if expected != OTHER_UUID else NATIVE_UUID)
            if self.outcome == "wrong_result"
            else expected
        )
        return ExternalTurnResult("claude", "Fictional visible answer", returned, "example-model")


class ClaudeNativeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = worker_fixtures.ExternalQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        claude = replace(
            self.fixture.config.agents[0],
            agent_id="claude",
            display_name="Claude",
            telegram_username="example_claude_bot",
            runtime="claude",
            executable="example-claude",
        )
        self.fixture.config = replace(
            self.fixture.config,
            agents=(claude,),
            external_worker_agent_ids=("claude",),
            outbox_runtime="external",
        )
        self.path = self.fixture.config.state_path
        self.root = self.fixture.registry.projects[0].root.resolve()

    def enqueue(self, message_id: int) -> str:
        return self.fixture.enqueue("claude", message_id, thread_id=77)

    def worker(self, adapter: ObservingClaudeAdapter) -> ExternalQueueWorker:
        worker = ExternalQueueWorker(
            self.fixture.config,
            "claude",
            registry=self.fixture.registry,
            adapter=cast(Any, adapter),
            worker_id="example-claude-worker",
        )
        self.addCleanup(worker.close)
        return worker

    def assert_binding_visible_before_invocation(
        self, adapter: ObservingClaudeAdapter, index: int = 0
    ) -> str:
        call = adapter.calls[index]
        expected = call["session_id"] or call["new_session_id"]
        self.assertIsInstance(expected, str)
        self.assertEqual(str(uuid.UUID(expected)), expected)
        self.assertEqual(adapter.observed[index], (expected, (expected, str(self.root), None)))
        return expected

    def assert_no_result(self, state: HubState, job_id: str) -> None:
        self.assertIsNone(
            state._connection.execute(
                "SELECT result_id FROM provider_job_results WHERE job_id=?", (job_id,)
            ).fetchone()
        )

    def assert_uncertain_and_not_replayed(
        self, worker: ExternalQueueWorker, adapter: ObservingClaudeAdapter, job_id: str
    ) -> None:
        job = worker.state.get_provider_job(job_id)
        self.assertEqual(job.status, "indeterminate")
        self.assertIsNotNone(
            persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
        )
        self.assert_no_result(worker.state, job_id)
        self.assertFalse(worker.run_cycle())
        self.assertEqual(len(adapter.calls), 1)

    def test_first_invocation_receives_new_uuid_already_committed_with_root(self) -> None:
        job_id = self.enqueue(1)
        adapter = ObservingClaudeAdapter(self.path)
        worker = self.worker(adapter)

        self.assertTrue(worker.run_cycle())

        self.assertEqual(len(adapter.calls), 1)
        self.assertIsNone(adapter.calls[0]["session_id"])
        native = self.assert_binding_visible_before_invocation(adapter)
        self.assertEqual(adapter.calls[0]["new_session_id"], native)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(worker.state.get_provider_result(job_id).provider_session_id, native)
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["provider_turn_id"])

    def test_runtime_policy_drift_retains_partial_and_root_without_completion_or_replay(
        self,
    ) -> None:
        job_id = self.enqueue(1)
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            native = argv[argv.index("--session-id") + 1]
            events = (
                {
                    "type": "assistant",
                    "session_id": native,
                    "uuid": OTHER_UUID,
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "text", "text": "Saved incomplete answer"}]},
                },
                {
                    "type": "control_request",
                    "request": {"subtype": "can_use_tool", "input": "private payload"},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": native,
                    "result": "Must not be committed",
                },
            )
            return subprocess.CompletedProcess(argv, 0, "\n".join(map(json.dumps, events)), "")

        adapter = ExternalCliAdapter("claude", run=fake_run)
        worker = self.worker(cast(Any, adapter))
        with patch.dict(
            "os.environ",
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
            clear=True,
        ):
            self.assertTrue(worker.run_cycle())
            self.assertFalse(worker.run_cycle())
        job = worker.state.get_provider_job(job_id)
        self.assertEqual(job.status, "indeterminate")
        self.assertIsNotNone(
            persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
        )
        self.assert_no_result(worker.state, job_id)
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["completed_text"])
        self.assertEqual(
            ExecutionJournal(worker.state).partial_text(job_id), "Saved incomplete answer"
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private payload", job.error_detail or "")

    def test_failed_cli_preflight_never_invokes_and_next_request_keeps_allocated_uuid(self) -> None:
        child = self.root / "fictional-claude"
        marker = self.root / "productive-invocations"

        def install(help_text: str) -> None:
            child.write_text(
                f"#!{sys.executable}\nimport sys,json,pathlib\n"
                f"if sys.argv[1:] == ['--help']:\n    print({help_text!r})\n    sys.exit(0)\n"
                "native=sys.argv[sys.argv.index('--session-id')+1]\n"
                f"pathlib.Path({str(marker)!r}).write_text(native)\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':native,'result':'Visible answer'}))\n",
                encoding="utf-8",
            )
            child.chmod(0o700)

        install("Usage: fictional-claude\nOptions:\n")
        first = self.enqueue(1)
        adapter = ExternalCliAdapter("claude", executable=str(child))
        worker = self.worker(cast(Any, adapter))
        with patch.dict(
            "os.environ",
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
            clear=True,
        ):
            worker.run_cycle()
            failed = worker.state.get_provider_job(first)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error_class, "pre_execution")
            self.assertEqual(failed.error_code, "claude_cli_capabilities_unverified")
            self.assertFalse(marker.exists())
            initial = ExecutionJournal(worker.state).read(first)
            assert initial is not None
            native = initial["provider_thread_id"]
            self.assert_no_result(worker.state, first)
            install(HELP)
            second = self.enqueue(2)
            self.assertTrue(worker.run_cycle())
            self.assertEqual(marker.read_text(), native)
            self.assertEqual(worker.state.get_provider_job(first).status, "failed")
            self.assertEqual(worker.state.get_provider_job(second).status, "result_ready")

    def test_already_queued_none_snapshot_resumes_previous_native_uuid(self) -> None:
        first = self.enqueue(1)
        second = self.enqueue(2)
        adapter = ObservingClaudeAdapter(self.path)
        worker = self.worker(adapter)
        self.assertIsNone(worker.state.get_provider_job(second).provider_session_id)
        worker.run_cycle()
        # Simulate an acknowledged result delivery; provider session and provenance remain.
        with worker.state._connection:
            worker.state._connection.execute(
                "UPDATE provider_jobs SET status='completed' WHERE job_id=?", (first,)
            )
            worker.state._connection.execute(
                "UPDATE telegram_outbox SET status='delivered' WHERE job_id=?", (first,)
            )

        self.assertTrue(worker.run_cycle())

        self.assertEqual(len(adapter.calls), 2)
        native = self.assert_binding_visible_before_invocation(adapter, 0)
        self.assertEqual(adapter.calls[1]["session_id"], native)
        self.assertIsNone(adapter.calls[1]["new_session_id"])
        self.assert_binding_visible_before_invocation(adapter, 1)
        self.assertEqual(worker.state.get_provider_job(second).status, "result_ready")

    def test_changed_model_and_effort_resume_exact_completed_native_session(self) -> None:
        first = self.enqueue(1)
        adapter = ObservingClaudeAdapter(self.path)
        worker = self.worker(adapter)
        self.assertTrue(worker.run_cycle())
        first_job = worker.state.get_provider_job(first)
        native = self.assert_binding_visible_before_invocation(adapter)
        with worker.state._connection:
            worker.state._connection.execute(
                "UPDATE provider_jobs SET status='completed' WHERE job_id=?", (first,)
            )
            worker.state._connection.execute(
                "UPDATE telegram_outbox SET status='delivered' WHERE job_id=?", (first,)
            )
        prior_checkpoint = ExecutionJournal(worker.state).read(first)
        selected = worker.state.replace_active_session(
            first_job.topic_id,
            model="example-next",
            effort="medium",
            runtime="claude",
            expected_session_id=first_job.session_id,
        )
        self.assertEqual(len(adapter.calls), 1)  # Selection itself invokes no provider.
        second, created = worker.state.enqueue_provider_job(
            idempotency_key="example-second-selection",
            chat_id=first_job.chat_id,
            message_id=2,
            topic_id=first_job.topic_id,
            agent_id=selected.agent_id,
            session_id=selected.session_id,
            session_generation=selected.generation,
            provider_session_id=selected.provider_session_id,
            model=selected.model,
            effort=selected.effort,
            payload_text="Fictional next request",
        )
        self.assertTrue(created)
        self.assertTrue(worker.run_cycle())
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(adapter.calls[1]["session_id"], native)
        self.assertIsNone(adapter.calls[1]["new_session_id"])
        self.assertEqual(
            (adapter.calls[1]["model"], adapter.calls[1]["effort"]), ("example-next", "medium")
        )
        self.assertEqual(selected.session_id, first_job.session_id)
        self.assertEqual(selected.generation, first_job.session_generation)
        self.assertEqual(ExecutionJournal(worker.state).read(first), prior_checkpoint)
        self.assertEqual(
            worker.state.get_provider_job(first), replace(first_job, status="completed")
        )
        self.assertEqual(worker.state.get_provider_job(second.job_id).status, "result_ready")

    def test_missing_native_root_provenance_refuses_before_adapter_invocation(self) -> None:
        job_id = self.enqueue(1)
        state = HubState.open(self.path, codex_permission_profile=None)
        job = state.get_provider_job(job_id)
        with state._connection:
            state._connection.execute(
                "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                (f"root:{self.root}", job.topic_id),
            )
        state.bind_provider_session(job.session_id, NATIVE_UUID, None)
        state.close()
        adapter = ObservingClaudeAdapter(self.path)
        worker = self.worker(adapter)

        worker.run_cycle()

        self.assertEqual(adapter.calls, [])
        self.assert_no_result(worker.state, job_id)
        self.assertIsNone(ExecutionJournal(worker.state).read(job_id))
        refused = worker.state.get_provider_job(job_id)
        self.assertEqual(
            (refused.status, refused.error_class, refused.error_code),
            ("failed", "pre_execution", "claude_session_preparation_failed"),
        )
        notice = worker.state.get_telegram_outbox_for_job(job_id)
        self.assertIn("not started", notice.telegram_html.casefold())
        self.assertEqual(worker.state.get_session(job.session_id).provider_session_id, NATIVE_UUID)

    def test_conflicting_native_root_provenance_refuses_before_adapter_invocation(self) -> None:
        prior_id = self.enqueue(1)
        state = HubState.open(self.path, codex_permission_profile=None)
        prior = state.lease_provider_job("claude", "fictional-prior-worker")
        assert prior is not None and prior.lease_token is not None
        state.mark_provider_job_executing(prior_id, prior.lease_token)
        with state._connection:
            state._connection.execute(
                "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                (f"root:{self.root}", prior.topic_id),
            )
        other_root = self.root.parent / "historical-root"
        other_root.mkdir()
        ExecutionJournal(state).record_thread(prior_id, prior.lease_token, NATIVE_UUID, other_root)
        with state._connection:
            state._connection.execute(
                "UPDATE provider_jobs SET status='completed',lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL WHERE job_id=?",
                (prior_id,),
            )
        state.close()
        job_id = self.enqueue(2)
        adapter = ObservingClaudeAdapter(self.path)
        worker = self.worker(adapter)

        worker.run_cycle()

        self.assertEqual(adapter.calls, [])
        self.assert_no_result(worker.state, job_id)
        self.assertIsNone(ExecutionJournal(worker.state).read(job_id))
        refused = worker.state.get_provider_job(job_id)
        self.assertEqual(
            (refused.status, refused.error_class, refused.error_code),
            ("failed", "pre_execution", "claude_session_preparation_failed"),
        )
        notice = worker.state.get_telegram_outbox_for_job(job_id)
        self.assertIn("not started", notice.telegram_html.casefold())
        checkpoint = ExecutionJournal(worker.state).read(prior_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["project_root"], str(other_root))

    def test_exact_terminal_quota_failure_is_failed_without_invented_quota_telemetry(self) -> None:
        job_id = self.enqueue(1)
        adapter = ObservingClaudeAdapter(self.path, outcome="quota")
        worker = self.worker(adapter)

        worker.run_cycle()

        job = worker.state.get_provider_job(job_id)
        self.assertEqual((job.status, job.error_code), ("failed", "claude_quota_exhausted"))
        self.assert_binding_visible_before_invocation(adapter)
        self.assert_no_result(worker.state, job_id)
        self.assertIsNone(persistent_root_blocker(worker.state._connection, topic_id=job.topic_id))
        health = worker.state.get_runtime_health("provider_worker", "example-claude-worker")
        assert health is not None
        self.assertEqual(health.provider_state, "limited")
        self.assertIsNone(health.quota_remaining_percent)
        self.assertIsNone(health.quota_reset_at)
        self.assertFalse(worker.run_cycle())
        self.assertEqual(len(adapter.calls), 1)

    def test_terminal_failure_with_wrong_uuid_retains_uncertainty(self) -> None:
        job_id = self.enqueue(1)
        adapter = ObservingClaudeAdapter(self.path, outcome="mismatched_failure")
        worker = self.worker(adapter)
        worker.run_cycle()
        self.assert_uncertain_and_not_replayed(worker, adapter, job_id)
        self.assert_binding_visible_before_invocation(adapter)

    def test_contradictory_structured_stream_retains_uncertainty(self) -> None:
        job_id = self.enqueue(1)
        adapter = ObservingClaudeAdapter(self.path, outcome="contradictory")
        worker = self.worker(adapter)
        worker.run_cycle()
        self.assert_uncertain_and_not_replayed(worker, adapter, job_id)
        self.assert_binding_visible_before_invocation(adapter)

    def test_covering_stop_wins_exact_proven_terminal_failure(self) -> None:
        job_id = self.enqueue(1)
        adapter = ObservingClaudeAdapter(self.path, outcome="quota", stop=True)
        worker = self.worker(adapter)
        worker.run_cycle()

        job = worker.state.get_provider_job(job_id)
        self.assertEqual((job.status, job.error_code), ("cancelled", "emergency_stop"))
        health = worker.state.get_runtime_health("provider_worker", "example-claude-worker")
        assert health is not None
        self.assertEqual((health.provider_state, health.error_code), ("ready", None))
        self.assert_binding_visible_before_invocation(adapter)
        self.assertEqual(
            worker.state._connection.execute(
                "SELECT status FROM provider_stop_requests WHERE topic_id=?", (job.topic_id,)
            ).fetchone()[0],
            "completed",
        )
        self.assertIsNone(persistent_root_blocker(worker.state._connection, topic_id=job.topic_id))
        self.assert_no_result(worker.state, job_id)
        self.assertFalse(worker.run_cycle())
        self.assertEqual(len(adapter.calls), 1)

    def test_success_returning_wrong_uuid_cannot_commit_result_or_replace_binding(self) -> None:
        job_id = self.enqueue(1)
        adapter = ObservingClaudeAdapter(self.path, outcome="wrong_result")
        worker = self.worker(adapter)
        worker.run_cycle()

        self.assert_uncertain_and_not_replayed(worker, adapter, job_id)
        native = self.assert_binding_visible_before_invocation(adapter)
        job = worker.state.get_provider_job(job_id)
        self.assertEqual(worker.state.get_session(job.session_id).provider_session_id, native)


if __name__ == "__main__":
    unittest.main()
