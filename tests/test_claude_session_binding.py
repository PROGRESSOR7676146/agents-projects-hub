from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState, ProviderJobRecord, StateError

NATIVE_UUID = "00000000-0000-4000-8000-000000000001"
OTHER_UUID = "019abcde-1234-7fff-8fff-0123456789ab"


class ClaudeSessionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "example-project"
        self.root.mkdir()
        self.path = self.base / "state.db"
        self.state = HubState.open(self.path)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            title="Fictional Claude topic",
            execution_root=self.root,
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "claude", "example-model", "high"
        )
        self.journal = ExecutionJournal(self.state)

    def enqueue(self, message_id: int, snapshot: str | None = None) -> ProviderJobRecord:
        job, created = self.state.enqueue_provider_job(
            idempotency_key=f"example:{message_id}",
            chat_id=self.topic.chat_id,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id=self.session.agent_id,
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            provider_session_id=snapshot,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Fictional Claude request",
        )
        self.assertTrue(created)
        return job

    def execute(self, job: ProviderJobRecord) -> str:
        leased = self.state.lease_provider_job("claude", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.assertEqual(leased.job_id, job.job_id)
        self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        return leased.lease_token

    def finish_prior(self, job: ProviderJobRecord, token: str) -> None:
        # Seed a previous accepted invocation without a provider or delivery adapter.
        self.journal.record_thread(job.job_id, token, NATIVE_UUID, self.root)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='completed', lease_token=NULL, "
                "lease_owner=NULL, lease_expires_at=NULL WHERE job_id=?",
                (job.job_id,),
            )

    def resume_fixture(self, snapshot: str | None = None) -> tuple[ProviderJobRecord, str, str]:
        prior = self.enqueue(1)
        token = self.execute(prior)
        self.finish_prior(prior, token)
        current = self.enqueue(2, snapshot)
        return current, self.execute(current), prior.job_id

    def prepare(self, job: ProviderJobRecord, token: str, cwd: Path | None = None) -> Any:
        return self.journal.prepare_claude_session(job.job_id, token, cwd or self.root)

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        return {
            table: [tuple(row) for row in self.state._connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "agent_sessions",
                "provider_jobs",
                "provider_execution_checkpoints",
            )
        }

    def assert_refused(self, job: ProviderJobRecord, token: str, cwd: Path | None = None) -> None:
        before = self.snapshot()
        with self.assertRaises((StateError, OSError)):
            self.prepare(job, token, cwd)
        self.assertEqual(self.snapshot(), before)

    def test_new_binding_is_immutable_and_committed_before_return(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        binding = self.prepare(job, token)

        self.assertEqual(type(binding).__name__, "ClaudeSessionBinding")
        self.assertIs(binding.is_new, True)
        self.assertEqual(str(uuid.UUID(binding.session_id)), binding.session_id)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            setattr(binding, "session_id", OTHER_UUID)
        with sqlite3.connect(self.path) as observer:
            self.assertEqual(
                observer.execute(
                    "SELECT provider_session_id FROM agent_sessions WHERE session_id=?",
                    (self.session.session_id,),
                ).fetchone()[0],
                binding.session_id,
            )
            checkpoint = observer.execute(
                "SELECT provider_thread_id,project_root,provider_turn_id "
                "FROM provider_execution_checkpoints WHERE job_id=?",
                (job.job_id,),
            ).fetchone()
        self.assertEqual(checkpoint, (binding.session_id, str(self.root.resolve()), None))
        self.assertIsNone(self.state.get_provider_job(job.job_id).provider_session_id)

    def test_new_binding_records_canonical_cwd(self) -> None:
        alias = self.base / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        job = self.enqueue(1)
        self.prepare(job, self.execute(job), alias)
        checkpoint = self.journal.read(job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["project_root"], str(self.root.resolve()))

    def test_nonarchived_satellite_telegram_session_can_allocate(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET status='satellite' WHERE session_id=?",
                (self.session.session_id,),
            )
        self.assertIs(self.prepare(job, token).is_new, True)

    def test_queued_none_snapshot_resumes_uuid_allocated_by_earlier_generation_peer(self) -> None:
        prior = self.enqueue(1)
        current = self.enqueue(2)
        self.assertIsNone(current.provider_session_id)
        self.finish_prior(prior, self.execute(prior))

        binding = self.prepare(current, self.execute(current))

        self.assertEqual((binding.session_id, binding.is_new), (NATIVE_UUID, False))
        checkpoint = self.journal.read(current.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["provider_thread_id"], NATIVE_UUID)
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertIsNone(self.state.get_provider_job(current.job_id).provider_session_id)

    def test_explicit_snapshot_resumes_only_exact_current_uuid_with_provenance(self) -> None:
        job, token, _ = self.resume_fixture(NATIVE_UUID)
        binding = self.prepare(job, token)
        self.assertEqual((binding.session_id, binding.is_new), (NATIVE_UUID, False))

    def test_resume_accepts_alias_only_when_canonical_root_matches_provenance(self) -> None:
        job, token, _ = self.resume_fixture()
        alias = self.base / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        binding = self.prepare(job, token, alias)
        self.assertEqual((binding.session_id, binding.is_new), (NATIVE_UUID, False))

    def test_existing_current_uuid_without_provenance_is_not_laundered(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        self.state.bind_provider_session(self.session.session_id, NATIVE_UUID, None)
        self.assert_refused(job, token)
        self.assertIsNone(self.journal.read(job.job_id))

    def test_explicit_snapshot_must_match_current_uuid(self) -> None:
        job, token, _ = self.resume_fixture(NATIVE_UUID)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
                (OTHER_UUID, job.job_id),
            )
        self.assert_refused(job, token)

    def test_explicit_snapshot_with_no_current_uuid_does_not_allocate(self) -> None:
        self.state.bind_provider_session(self.session.session_id, NATIVE_UUID, None)
        job = self.enqueue(1, NATIVE_UUID)
        token = self.execute(job)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET provider_session_id=NULL WHERE session_id=?",
                (self.session.session_id,),
            )
        self.assert_refused(job, token)

    def test_resume_rejects_a_different_current_root_without_creating_checkpoint(self) -> None:
        job, token, _ = self.resume_fixture()
        other_root = self.base / "other-project-root"
        other_root.mkdir()
        self.assert_refused(job, token, other_root)
        self.assertIsNone(self.journal.read(job.job_id))

    def test_missing_provenance_checkpoint_does_not_authorize_resume(self) -> None:
        job, token, prior_id = self.resume_fixture()
        with self.state._connection:
            self.state._connection.execute(
                "DELETE FROM provider_execution_checkpoints WHERE job_id=?", (prior_id,)
            )
        self.assert_refused(job, token)

    def test_provenance_must_match_hub_session_generation_agent_topic_uuid_and_root(self) -> None:
        job, token, prior_id = self.resume_fixture()
        other_topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=self.topic.chat_id,
            thread_id=8,
            title="Other fictional topic",
            execution_root=self.root,
        )
        other_session = self.state.activate_agent(other_topic.topic_id, "claude", "model", "high")
        cases = (
            ("provider_jobs", "session_id", other_session.session_id),
            ("provider_jobs", "session_generation", self.session.generation + 1),
            ("provider_jobs", "agent_id", "codex"),
            ("provider_jobs", "topic_id", other_topic.topic_id),
            ("provider_execution_checkpoints", "provider_thread_id", OTHER_UUID),
            ("provider_execution_checkpoints", "project_root", str(self.base)),
        )
        for table, column, value in cases:
            with self.subTest(table=table, column=column):
                old = self.state._connection.execute(
                    f"SELECT {column} FROM {table} WHERE job_id=?", (prior_id,)
                ).fetchone()[0]
                with self.state._connection:
                    self.state._connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE job_id=?", (value, prior_id)
                    )
                try:
                    self.assert_refused(job, token)
                finally:
                    with self.state._connection:
                        self.state._connection.execute(
                            f"UPDATE {table} SET {column}=? WHERE job_id=?", (old, prior_id)
                        )

    def test_prepare_again_for_same_job_never_grants_another_invocation(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        self.prepare(job, token)
        self.assert_refused(job, token)

    def test_existing_same_job_checkpoint_cannot_supply_its_own_resume_provenance(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        self.journal.record_thread(job.job_id, token, NATIVE_UUID, self.root)
        self.assert_refused(job, token)

    def test_current_invocation_requires_matching_session_ownership(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        other_topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=self.topic.chat_id,
            thread_id=8,
            title="Other fictional topic",
            execution_root=self.root,
        )
        cases = (
            ("generation", self.session.generation + 1),
            ("agent_id", "codex"),
            ("topic_id", other_topic.topic_id),
            ("writer_mode", "local"),
            ("writer_mode", "terminal"),
            ("status", "archived"),
        )
        for column, value in cases:
            with self.subTest(column=column, value=value):
                old = self.state._connection.execute(
                    f"SELECT {column} FROM agent_sessions WHERE session_id=?",
                    (self.session.session_id,),
                ).fetchone()[0]
                with self.state._connection:
                    self.state._connection.execute(
                        f"UPDATE agent_sessions SET {column}=? WHERE session_id=?",
                        (value, self.session.session_id),
                    )
                try:
                    self.assert_refused(job, token)
                finally:
                    with self.state._connection:
                        self.state._connection.execute(
                            f"UPDATE agent_sessions SET {column}=? WHERE session_id=?",
                            (old, self.session.session_id),
                        )

    def assert_prepare_refuses_topic_drift(self, *, resume: bool, changed_scope: bool) -> None:
        if resume:
            job, token, _ = self.resume_fixture()
        else:
            job = self.enqueue(1)
            token = self.execute(job)
        with self.state._connection:
            if changed_scope:
                self.state._connection.execute(
                    "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                    (f"root:{self.base.resolve()}", self.topic.topic_id),
                )
            else:
                self.state._connection.execute(
                    "UPDATE provider_jobs SET chat_id=? WHERE job_id=?",
                    (-1002222222222, job.job_id),
                )
        before = tuple(self.state._connection.iterdump())
        with self.assertRaises(StateError):
            self.prepare(job, token)
        self.assertEqual(tuple(self.state._connection.iterdump()), before)

    def test_new_preparation_refuses_changed_topic_scope_without_db_changes(self) -> None:
        self.assert_prepare_refuses_topic_drift(resume=False, changed_scope=True)

    def test_resumed_preparation_refuses_changed_topic_scope_without_db_changes(self) -> None:
        self.assert_prepare_refuses_topic_drift(resume=True, changed_scope=True)

    def test_new_preparation_refuses_job_topic_chat_mismatch_without_db_changes(self) -> None:
        self.assert_prepare_refuses_topic_drift(resume=False, changed_scope=False)

    def test_resumed_preparation_refuses_job_topic_chat_mismatch_without_db_changes(self) -> None:
        self.assert_prepare_refuses_topic_drift(resume=True, changed_scope=False)

    def test_expired_wrong_or_nonexecuting_lease_refuses_atomically(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        self.assert_refused(job, "not-the-current-lease")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='leased' WHERE job_id=?", (job.job_id,)
            )
        self.assert_refused(job, token)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='executing',lease_expires_at=? WHERE job_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job.job_id),
            )
        self.assert_refused(job, token)

    def test_malformed_current_or_snapshot_uuid_refuses_without_repair(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        for column_target in ("agent_sessions", "provider_jobs"):
            for value in ("", "not-a-uuid", NATIVE_UUID + "/suffix"):
                with self.subTest(target=column_target, value=value):
                    identity_column = (
                        "session_id" if column_target == "agent_sessions" else "job_id"
                    )
                    identity = (
                        self.session.session_id if column_target == "agent_sessions" else job.job_id
                    )
                    with self.state._connection:
                        self.state._connection.execute(
                            f"UPDATE {column_target} SET provider_session_id=? WHERE {identity_column}=?",
                            (value, identity),
                        )
                    try:
                        self.assert_refused(job, token)
                    finally:
                        with self.state._connection:
                            self.state._connection.execute(
                                f"UPDATE {column_target} SET provider_session_id=NULL WHERE {identity_column}=?",
                                (identity,),
                            )

    def test_missing_root_directory_refuses_before_state_mutation(self) -> None:
        job = self.enqueue(1)
        self.assert_refused(job, self.execute(job), self.base / "missing-project-root")

    def test_session_or_checkpoint_write_fault_rolls_back_entire_binding(self) -> None:
        job = self.enqueue(1)
        token = self.execute(job)
        for operation in ("UPDATE ON agent_sessions", "INSERT ON provider_execution_checkpoints"):
            with self.subTest(operation=operation):
                with self.state._connection:
                    self.state._connection.execute(
                        f"CREATE TRIGGER binding_fault BEFORE {operation} "
                        "BEGIN SELECT RAISE(ABORT, 'fictional binding fault'); END"
                    )
                before = self.snapshot()
                try:
                    with self.assertRaises((sqlite3.DatabaseError, StateError)):
                        self.prepare(job, token)
                    self.assertEqual(self.snapshot(), before)
                finally:
                    with self.state._connection:
                        self.state._connection.execute("DROP TRIGGER binding_fault")


if __name__ == "__main__":
    unittest.main()
