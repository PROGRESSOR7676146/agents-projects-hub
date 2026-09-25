from __future__ import annotations

import unittest
from typing import Any, cast

import test_codex_worker as fixtures

from hermes_codex_router.codex_appserver import (
    CodexThreadMetadata,
    CodexTurnError,
    RpcError,
    StoredTurnOutcome,
    TurnResult,
)
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.existing_local_reconciliation import reconcile_existing_local
from hermes_codex_router.state import HubState, StateError


class ExistingLocalReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.job_id = self.fixture.enqueue(provider_session_id="thread-1")

        class Client(fixtures.WorkerClient):
            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise CodexTurnError(RpcError("fictional transport failure"))

            def read_completed_turn(self, **_kwargs: object) -> None:
                return None

        class Supervisor(fixtures.WorkerSupervisor):
            transport_mode = "socket"

        worker = CodexQueueWorker(
            self.fixture.config,
            registry=self.fixture.registry,
            supervisor=cast(Any, Supervisor(Client())),
            worker_id="fictional-reconcile-worker",
        )
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(self.job_id).status, "indeterminate")
        finally:
            worker.close()

        state = HubState.open(self.fixture.config.state_path)
        try:
            old = state.get_provider_job(self.job_id)
            self.session_id = old.session_id
            self.generation = old.session_generation
            self.original_error = (old.error_class, old.error_code, old.error_detail)
        finally:
            state.close()
        self.root = self.fixture.registry.projects[0].root
        self.reads = 0

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def inspector(self, _config: Any, thread: str, turn: str, root: Any):
        self.reads += 1
        self.assertEqual((thread, turn, root), ("thread-1", "turn-1", self.root))
        return CodexThreadMetadata(thread, root, "openai", "idle"), StoredTurnOutcome("failed")

    def reconcile(self, **overrides: Any):
        arguments = {
            "session_id": self.session_id,
            "provider_thread_id": "thread-1",
            "old_job_id": self.job_id,
            "expected_generation": self.generation,
            "expected_root": self.root,
            "inspector": self.inspector,
        }
        arguments.update(overrides)
        return reconcile_existing_local(self.fixture.config, **arguments)

    def test_preview_and_exact_closed_cli_claim_preserve_old_job_and_session(self) -> None:
        preview = self.reconcile()
        self.assertEqual((preview.writer_mode, preview.old_turn_status), ("telegram", "failed"))
        self.assertFalse(preview.changed)
        with self.assertRaisesRegex(StateError, "ownership boundary"):
            self.reconcile(apply=True)
        applied = self.reconcile(apply=True, confirm_cli_closed=True)
        self.assertEqual((applied.writer_mode, applied.old_job_status), ("local", "indeterminate"))
        self.assertIsNotNone(applied.resume_command)
        assert applied.resume_command is not None
        self.assertIn("--remote", applied.resume_command)
        self.assertIn("thread-1", applied.resume_command)
        self.assertTrue(applied.changed)
        repeated = self.reconcile(apply=True, confirm_cli_closed=True)
        self.assertFalse(repeated.changed)
        state = HubState.open(self.fixture.config.state_path)
        try:
            old = state.get_provider_job(self.job_id)
            session = state.get_session(self.session_id)
            self.assertEqual(
                (old.error_class, old.error_code, old.error_detail), self.original_error
            )
            self.assertEqual(session.provider_session_id, "thread-1")
            self.assertEqual(session.writer_mode, "local")
            self.assertEqual(len(state.provider_jobs_for_topic(old.topic_id)), 1)
            self.assertIsNone(state.lease_provider_job("codex", "fictional-worker"))
        finally:
            state.close()

    def test_wrong_thread_root_generation_and_busy_turn_fail_closed(self) -> None:
        for changed in (
            {"provider_thread_id": "other-thread"},
            {"expected_generation": self.generation + 1},
            {"expected_root": self.root.parent},
        ):
            with self.subTest(changed=changed), self.assertRaises(Exception):
                self.reconcile(apply=True, confirm_cli_closed=True, **changed)

        def active(_config: Any, thread: str, _turn: str, root: Any):
            return CodexThreadMetadata(thread, root, "openai", "idle"), StoredTurnOutcome("active")

        with self.assertRaisesRegex(StateError, "idle terminal turn"):
            self.reconcile(apply=True, confirm_remote_idle=True, inspector=active)
        state = HubState.open(self.fixture.config.state_path)
        try:
            self.assertEqual(state.get_session(self.session_id).writer_mode, "telegram")
        finally:
            state.close()

    def test_queued_tail_is_held_during_closed_cli_reconciliation(self) -> None:
        state = HubState.open(self.fixture.config.state_path)
        try:
            old = state.get_provider_job(self.job_id)
            # Recreate an already accepted pre-upgrade tail. New admission
            # correctly rejects input while this old turn is unresolved.
            with state._connection:
                state._connection.execute(
                    """INSERT INTO provider_jobs
                       (job_id,idempotency_key,chat_id,message_id,topic_id,topic_sequence,
                        agent_id,session_id,session_generation,provider_session_id,
                        model,effort,payload_text,status,attempt_count,max_attempts,
                        created_at,updated_at)
                       SELECT 'fictional-tail','telegram:fictional-tail',chat_id,18,topic_id,
                              topic_sequence+1,agent_id,session_id,session_generation,
                              provider_session_id,model,effort,'Fictional queued tail',
                              'queued',0,5,created_at,updated_at
                       FROM provider_jobs WHERE job_id=?""",
                    (old.job_id,),
                )
            tail = state.get_provider_job("fictional-tail")
        finally:
            state.close()
        result = self.reconcile(apply=True, confirm_cli_closed=True)
        self.assertEqual(result.writer_mode, "local")
        state = HubState.open(self.fixture.config.state_path)
        try:
            held = state._connection.execute(
                "SELECT cause_job_id FROM provider_job_holds WHERE job_id = ?", (tail.job_id,)
            ).fetchone()
            self.assertIsNotNone(held)
            assert held is not None
            self.assertEqual(held["cause_job_id"], self.job_id)
            self.assertEqual(state.get_provider_job(tail.job_id).status, "queued")
            self.assertIsNone(state.lease_provider_job("codex", "fictional-worker"))
        finally:
            state.close()

    def test_remote_idle_assertion_and_other_topic_writer_on_same_root(self) -> None:
        state = HubState.open(self.fixture.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=78,
                title="Other fictional topic",
                execution_root=self.root,
            )
            other = state.activate_agent(topic.topic_id, "codex", "gpt-5.6-sol", "high")
            with state._immediate_transaction():
                state._connection.execute(
                    "UPDATE agent_sessions SET writer_mode = 'local' WHERE session_id = ?",
                    (other.session_id,),
                )
        finally:
            state.close()
        with self.assertRaisesRegex(StateError, "another writer"):
            self.reconcile(apply=True, confirm_remote_idle=True)
        state = HubState.open(self.fixture.config.state_path)
        try:
            with state._immediate_transaction():
                state._connection.execute(
                    "UPDATE agent_sessions SET writer_mode = 'telegram' WHERE session_id = ?",
                    (other.session_id,),
                )
        finally:
            state.close()
        result = self.reconcile(apply=True, confirm_remote_idle=True)
        self.assertEqual(result.writer_mode, "local")
