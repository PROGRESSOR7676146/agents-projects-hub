from __future__ import annotations

import multiprocessing
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.codex_failure import MAX_PARTIAL_TEXT
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import RECOVERED_RESULT_METADATA_JSON, HubState
from tests import test_claude_native_worker as worker_fixtures

MESSAGE_UUID = "00000000-0000-4000-8000-000000000001"
OTHER_UUID = "019abcde-1234-7fff-8fff-0123456789ab"
SAVED_TEXT = "Fictional saved Claude completion."
PARTIAL_TEXT = "Fictional provisional Claude response."


def crash_after_completion(path: Path, job_id: str, token: str, native: str, root: Path) -> None:
    state = HubState.open(path)
    ExecutionJournal(state).record_claude_completion(job_id, token, native, SAVED_TEXT, cwd=root)
    os._exit(17)


class NoReplayAdapter:
    runtime = "claude"

    def __init__(self) -> None:
        self.calls = 0

    def run_turn(self, **_kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("recovery must never invoke or resume Claude")


class ClaudeRecoveryTests(unittest.TestCase):
    def seed(
        self,
        *,
        completion: str | None = SAVED_TEXT,
        partial: str | None = None,
        checkpoint: bool = True,
        expired: bool = True,
        context: bool = False,
    ) -> tuple[ExternalQueueWorker, NoReplayAdapter, str, str | None, Path, int | None]:
        fixture = worker_fixtures.ClaudeNativeWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = NoReplayAdapter()
        worker = fixture.worker(cast(Any, adapter))
        state = worker.state
        root = fixture.root
        topic = state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Fictional Claude recovery",
            execution_root=root,
        )
        session = state.activate_agent(topic.topic_id, "claude", "example-model", "high")
        watermark = (
            state.record_visible_turn(
                topic.topic_id,
                agent_id="opencode",
                provider="opencode",
                model="example-model",
                user_excerpt="Fictional prior request",
                response_excerpt="Fictional prior response",
            )
            if context
            else None
        )
        job, _ = state.enqueue_provider_job(
            idempotency_key="example-recovery:1",
            chat_id=topic.chat_id,
            message_id=1,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Fictional accepted Claude task",
            context_watermark=watermark,
        )
        leased = state.lease_provider_job("claude", "fictional-lost-worker")
        assert leased is not None and leased.lease_token is not None
        state.mark_provider_job_executing(job.job_id, leased.lease_token)
        journal = ExecutionJournal(state)
        native = None
        if checkpoint:
            native = journal.prepare_claude_session(job.job_id, leased.lease_token, root).session_id
            if partial is not None:
                journal.record_claude_item(
                    job.job_id,
                    leased.lease_token,
                    native,
                    MESSAGE_UUID,
                    partial,
                    cwd=root,
                )
            if completion is not None:
                journal.record_claude_completion(
                    job.job_id,
                    leased.lease_token,
                    native,
                    completion,
                    cwd=root,
                )
        if expired:
            state.heartbeat_provider_job(
                job.job_id,
                leased.lease_token,
                lease_seconds=1,
                now=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
        return worker, adapter, job.job_id, native, root, watermark

    def assert_no_replay(
        self, worker: ExternalQueueWorker, adapter: NoReplayAdapter, job_id: str
    ) -> None:
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(worker.state.get_provider_job(job_id).attempt_count, 1)
        self.assertFalse(worker.run_cycle())
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(worker.state.get_provider_job(job_id).attempt_count, 1)

    def assert_unknown(self, worker: ExternalQueueWorker, job_id: str) -> str:
        job = worker.state.get_provider_job(job_id)
        self.assertEqual(job.status, "indeterminate")
        self.assertIsNotNone(
            persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
        )
        self.assertIsNone(
            worker.state._connection.execute(
                "SELECT result_id FROM provider_job_results WHERE job_id=?", (job_id,)
            ).fetchone()
        )
        notice = worker.state.get_telegram_outbox_for_job(job_id)
        self.assertEqual(notice.sender_agent_id, "claude")
        return notice.telegram_html

    def test_durable_completion_recovers_result_artifact_and_context_without_replay(self) -> None:
        worker, adapter, job_id, native, root, watermark = self.seed(context=True)
        staging = root / ".hub" / "staging" / job_id
        staging.mkdir(parents=True)
        (staging / "report.md").write_text("# Fictional recovered artifact", encoding="utf-8")

        self.assertTrue(worker.run_cycle())

        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        result = worker.state.get_provider_result(job_id)
        self.assertEqual(
            (result.visible_response, result.provider_session_id), (SAVED_TEXT, native)
        )
        self.assertEqual(result.safe_metadata_json, RECOVERED_RESULT_METADATA_JSON)
        outbox = worker.state.get_telegram_outbox_for_job(job_id)
        parts = worker.state.get_telegram_outbox_parts(outbox.outbox_id)
        self.assertEqual([part.part_type for part in parts], ["text", "document"])
        assert parts[1].file_path is not None
        self.assertTrue(Path(parts[1].file_path).is_file())
        job = worker.state.get_provider_job(job_id)
        cursor = worker.state._connection.execute(
            "SELECT last_turn_id FROM visible_context_cursors WHERE topic_id=? AND observer_agent_id='claude'",
            (job.topic_id,),
        ).fetchone()
        self.assertEqual(cursor[0], watermark)
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assert_no_replay(worker, adapter, job_id)

    def test_abrupt_exit_after_completion_commit_recovers_without_provider_replay(self) -> None:
        worker, adapter, job_id, native, root, _ = self.seed(completion=None, expired=False)
        job = worker.state.get_provider_job(job_id)
        assert job.lease_token is not None and native is not None
        process = multiprocessing.get_context("fork").Process(
            target=crash_after_completion,
            args=(worker.config.state_path, job_id, job.lease_token, native, root),
        )
        process.start()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(3)
        self.assertEqual(process.exitcode, 17)
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], SAVED_TEXT)
        self.assertIsNone(checkpoint["provider_turn_id"])
        worker.state.heartbeat_provider_job(
            job_id,
            job.lease_token,
            lease_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=10),
        )

        self.assertTrue(worker.run_cycle())

        self.assertEqual(worker.state.get_provider_result(job_id).visible_response, SAVED_TEXT)
        self.assert_no_replay(worker, adapter, job_id)

    def test_full_saved_completion_is_not_truncated_to_partial_notice_limit(self) -> None:
        full = "x" * (MAX_PARTIAL_TEXT * 2) + "Z"
        worker, adapter, job_id, _, _, _ = self.seed(completion=full)

        self.assertTrue(worker.run_cycle())

        self.assertEqual(worker.state.get_provider_result(job_id).visible_response, full)
        self.assert_no_replay(worker, adapter, job_id)

    def test_partial_only_recovery_is_explicitly_incomplete_and_keeps_root(self) -> None:
        partial = "A" * (MAX_PARTIAL_TEXT + 100) + "saved tail"
        worker, adapter, job_id, _, _, _ = self.seed(completion=None, partial=partial, context=True)

        self.assertTrue(worker.run_cycle())

        notice = self.assert_unknown(worker, job_id)
        self.assertIn("incomplete", notice.casefold())
        self.assertIn("saved tail", notice)
        self.assertNotIn(partial, notice)
        self.assertLessEqual(len(notice), MAX_PARTIAL_TEXT + 2000)
        job = worker.state.get_provider_job(job_id)
        self.assertIsNone(
            worker.state._connection.execute(
                "SELECT last_turn_id FROM visible_context_cursors WHERE topic_id=? AND observer_agent_id='claude'",
                (job.topic_id,),
            ).fetchone()
        )
        self.assert_no_replay(worker, adapter, job_id)

    def test_bound_session_without_saved_terminal_outcome_remains_unknown(self) -> None:
        worker, adapter, job_id, _, _, _ = self.seed(completion=None)
        self.assertTrue(worker.run_cycle())
        self.assert_unknown(worker, job_id)
        self.assert_no_replay(worker, adapter, job_id)

    def test_missing_durable_checkpoint_never_authorizes_provider_replay(self) -> None:
        worker, adapter, job_id, _, _, _ = self.seed(checkpoint=False)
        self.assertTrue(worker.run_cycle())
        self.assert_unknown(worker, job_id)
        self.assert_no_replay(worker, adapter, job_id)

    def test_live_invocation_lease_is_not_recovered_or_replayed(self) -> None:
        worker, adapter, job_id, _, _, _ = self.seed(expired=False)
        before = worker.state.get_provider_job(job_id)
        self.assertFalse(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(job_id), before)
        self.assertEqual(adapter.calls, 0)

    def test_binding_mismatch_suppresses_saved_completion_and_partial_text(self) -> None:
        cases = (
            ("agent_sessions", "provider_session_id", OTHER_UUID),
            ("agent_sessions", "generation", 2),
            ("agent_sessions", "writer_mode", "local"),
            ("agent_sessions", "writer_mode", "terminal"),
            ("agent_sessions", "status", "archived"),
            ("agent_sessions", "agent_id", "opencode"),
            ("provider_jobs", "provider_session_id", OTHER_UUID),
            ("provider_execution_checkpoints", "provider_thread_id", OTHER_UUID),
            ("provider_execution_checkpoints", "project_root", "/home/example/other-root"),
            ("provider_execution_checkpoints", "provider_turn_id", MESSAGE_UUID),
        )
        for table, column, value in cases:
            with self.subTest(table=table, column=column, value=value):
                worker, adapter, job_id, _, _, _ = self.seed(partial=PARTIAL_TEXT)
                job = worker.state.get_provider_job(job_id)
                key = "session_id" if table == "agent_sessions" else "job_id"
                identity = job.session_id if table == "agent_sessions" else job_id
                with worker.state._connection:
                    worker.state._connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE {key}=?", (value, identity)
                    )

                self.assertTrue(worker.run_cycle())

                notice = self.assert_unknown(worker, job_id)
                self.assertNotIn(SAVED_TEXT, notice)
                self.assertNotIn(PARTIAL_TEXT, notice)
                self.assert_no_replay(worker, adapter, job_id)

    def test_missing_registered_root_suppresses_saved_output_without_replay(self) -> None:
        worker, adapter, job_id, _, root, _ = self.seed(partial=PARTIAL_TEXT)
        root.rename(root.with_name("fictional-moved-root"))
        self.assertTrue(worker.run_cycle())
        notice = self.assert_unknown(worker, job_id)
        self.assertNotIn(SAVED_TEXT, notice)
        self.assertNotIn(PARTIAL_TEXT, notice)
        self.assert_no_replay(worker, adapter, job_id)

    def test_covering_stop_wins_saved_completion_and_releases_root(self) -> None:
        worker, adapter, job_id, _, _, _ = self.seed()
        job = worker.state.get_provider_job(job_id)
        request_id, _, _ = worker.state.request_emergency_stop(
            topic_id=job.topic_id,
            chat_id=job.chat_id,
            message_id=2,
            target_agent_id="claude",
        )

        self.assertTrue(worker.run_cycle())

        self.assertEqual(worker.state.get_provider_job(job_id).status, "cancelled")
        self.assertIsNone(persistent_root_blocker(worker.state._connection, topic_id=job.topic_id))
        self.assertIsNone(
            worker.state._connection.execute(
                "SELECT result_id FROM provider_job_results WHERE job_id=?", (job_id,)
            ).fetchone()
        )
        self.assertEqual(
            worker.state._connection.execute(
                "SELECT status FROM provider_stop_requests WHERE request_id=?", (request_id,)
            ).fetchone()[0],
            "completed",
        )
        self.assert_no_replay(worker, adapter, job_id)

    def test_covering_stop_without_terminal_proof_keeps_uncertainty_and_stop_pending(self) -> None:
        worker, adapter, job_id, _, _, _ = self.seed(completion=None, partial=PARTIAL_TEXT)
        job = worker.state.get_provider_job(job_id)
        request_id, _, _ = worker.state.request_emergency_stop(
            topic_id=job.topic_id,
            chat_id=job.chat_id,
            message_id=2,
            target_agent_id="claude",
        )

        self.assertTrue(worker.run_cycle())

        self.assertIn(PARTIAL_TEXT, self.assert_unknown(worker, job_id))
        self.assertEqual(
            worker.state._connection.execute(
                "SELECT status FROM provider_stop_requests WHERE request_id=?", (request_id,)
            ).fetchone()[0],
            "pending",
        )
        self.assert_no_replay(worker, adapter, job_id)


if __name__ == "__main__":
    unittest.main()
