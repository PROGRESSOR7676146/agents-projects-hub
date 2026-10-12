"""Exact accepted-target control authority survives retries and runtime loss."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import HubState, StateError
from tests import test_codex_worker as fixtures
from tests.git_fixtures import init_git_root


class CodexTurnControlJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job_id = self.fixture.enqueue()
        self.state = HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.root = self.fixture.registry.projects[0].root
        job = self.state.get_provider_job(self.job_id)
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                (f"root:{self.root}", job.topic_id),
            )
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.token = lease.lease_token
        self.state.mark_provider_job_executing(self.job_id, self.token)
        self.journal = ExecutionJournal(self.state)
        self.journal.record_thread(self.job_id, self.token, "example-thread", self.root)

    def row(self):
        return self.state._connection.execute(
            "SELECT * FROM codex_turn_controls WHERE job_id=?", (self.job_id,)
        ).fetchone()

    def test_preparation_has_no_control_authority(self) -> None:
        self.assertIsNone(self.row())

    def test_incoherent_acceptance_retains_exact_identity_without_control_upgrade(self) -> None:
        job = self.state.get_provider_job(self.job_id)
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (job.session_id,),
            )
        with self.assertRaises(StateError):
            self.journal.record_turn(self.job_id, self.token, "example-turn")
        saved = self.journal.read(self.job_id)
        assert saved is not None
        self.assertEqual(saved["provider_turn_id"], "example-turn")
        self.assertIsNone(self.row())
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='telegram' WHERE session_id=?",
                (job.session_id,),
            )
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.assertIsNone(self.row())

    def test_first_exact_acceptance_atomically_freezes_control_binding(self) -> None:
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        control = self.row()
        assert control is not None
        job = self.state.get_provider_job(self.job_id)
        topic = self.state.get_topic(job.topic_id)
        self.assertEqual(control["origin"], "accepted_v48")
        self.assertEqual(control["provider_thread_id"], "example-thread")
        self.assertEqual(control["provider_turn_id"], "example-turn")
        self.assertEqual(control["project_root"], str(self.root))
        self.assertEqual(control["session_id"], job.session_id)
        self.assertEqual(control["session_generation"], job.session_generation)
        self.assertEqual(control["topic_id"], job.topic_id)
        self.assertEqual(control["project_id"], topic.project_id)
        self.assertEqual(control["chat_id"], job.chat_id)
        self.assertEqual(control["thread_id"], topic.thread_id)
        self.assertIsNone(control["codex_permission_profile"])
        self.assertIsNone(control["send_started_at"])
        self.assertIsNone(control["owner_quiesced_at"])
        self.assertEqual(control["late_read_attempts"], 0)

    def test_acceptance_failure_rolls_back_native_checkpoint(self) -> None:
        self.state._connection.execute(
            """CREATE TEMP TRIGGER example_refuse_control BEFORE INSERT ON codex_turn_controls
               BEGIN SELECT RAISE(ABORT, 'example control write fault'); END"""
        )
        self.state._connection.commit()
        with self.assertRaises(Exception):
            self.journal.record_turn(self.job_id, self.token, "example-turn")
        saved = self.journal.read(self.job_id)
        assert saved is not None
        self.assertIsNone(saved["provider_turn_id"])
        self.assertIsNone(self.row())

    def test_exact_native_target_cannot_gain_second_authority_on_another_root(self) -> None:
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.assert_duplicate_target_retained_without_authority()

    def test_declined_target_cannot_gain_authority_through_a_different_job(self) -> None:
        job = self.state.get_provider_job(self.job_id)
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (job.session_id,),
            )
        with self.assertRaises(StateError):
            self.journal.record_turn(self.job_id, self.token, "example-turn")
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='telegram' WHERE session_id=?",
                (job.session_id,),
            )
        self.assertIsNone(self.row())
        self.assert_duplicate_target_retained_without_authority()
        self.assertIsNone(self.row())

    def assert_duplicate_target_retained_without_authority(self) -> None:
        self.state.commit_provider_result(
            self.job_id,
            self.token,
            visible_response="Example first final",
            sender_agent_id="codex",
            telegram_html="Example first final",
        )
        other_root = self.root.parent / "example-other-root"
        init_git_root(other_root)
        topic = self.state.observe_topic(
            project_id="example-other-project",
            chat_id=-1001234567890,
            thread_id=78,
            title="Example independent root",
            execution_root=other_root,
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "example-model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="example-other-input",
            chat_id=topic.chat_id,
            message_id=2,
            topic_id=topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Example independent task",
        )
        leased = self.state.lease_provider_job("codex", "example-other-worker")
        assert leased is not None and leased.lease_token is not None
        self.assertEqual(leased.job_id, job.job_id)
        self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        self.journal.record_thread(job.job_id, leased.lease_token, "example-thread", other_root)
        with self.assertRaises(StateError):
            self.journal.record_turn(job.job_id, leased.lease_token, "example-turn")
        saved = self.journal.read(job.job_id)
        assert saved is not None
        self.assertEqual(saved["provider_turn_id"], "example-turn")
        self.assertIsNone(self.state.codex_controls.read(job.job_id))
        original = self.journal.read(self.job_id)
        assert original is not None
        self.assertEqual(original["project_root"], str(self.root))

    def test_repeat_record_turn_preserves_send_start_and_owner(self) -> None:
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        now = datetime.now(timezone.utc).isoformat()
        self.state._connection.execute(
            """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
               interrupt_source='protective' WHERE job_id=?""",
            (now, "example-control-owner", self.job_id),
        )
        self.state._connection.commit()
        before = dict(self.row())
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.assertEqual(dict(self.row()), before)

    def test_repeat_cannot_invent_authority_for_a_missing_historical_row(self) -> None:
        self.state._connection.execute(
            "UPDATE provider_execution_checkpoints SET provider_turn_id='example-turn' WHERE job_id=?",
            (self.job_id,),
        )
        self.state._connection.commit()
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.assertIsNone(self.row())

    def test_control_target_identity_is_immutable_even_after_job_completion(self) -> None:
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        for field, value in (
            ("provider_turn_id", "example-other-turn"),
            ("provider_thread_id", "example-other-thread"),
            ("session_generation", 999),
            ("project_root", "/home/example/other"),
            ("origin", "legacy_read_only"),
            ("codex_permission_profile", "example-other-policy"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(Exception):
                    with self.state._immediate_transaction():
                        self.state._connection.execute(
                            f"UPDATE codex_turn_controls SET {field}=? WHERE job_id=?",  # trusted literals
                            (value, self.job_id),
                        )
        self.assertEqual(self.row()["provider_turn_id"], "example-turn")

    def test_expired_invocation_lease_cannot_create_control_authority(self) -> None:
        self.state.heartbeat_provider_job(
            self.job_id,
            self.token,
            lease_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        with self.assertRaises(StateError):
            self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.assertIsNone(self.row())

    def test_covering_stop_before_acceptance_is_bound_without_new_stop_receipt(self) -> None:
        job = self.state.get_provider_job(self.job_id)
        request_id, _, pending = self.state.request_emergency_stop(
            topic_id=job.topic_id,
            chat_id=job.chat_id,
            message_id=99,
            target_agent_id="codex",
        )
        self.assertTrue(pending)
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.assertEqual(self.row()["stop_request_id"], request_id)
        self.assertIsNotNone(self.row()["next_late_read_at"])
        count = self.state._connection.execute(
            "SELECT count(*) FROM provider_stop_requests"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_terminal_job_never_releases_an_unquiesced_control_owner(self) -> None:
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        self.state._connection.execute(
            """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
               interrupt_source='protective' WHERE job_id=?""",
            (datetime.now(timezone.utc).isoformat(), "example-control-owner", self.job_id),
        )
        self.state._connection.commit()
        job = self.state.get_provider_job(self.job_id)
        for status in ("indeterminate", "result_ready", "completed", "cancelled", "failed"):
            with self.subTest(status=status):
                with self.state._immediate_transaction():
                    self.state._connection.execute(
                        """UPDATE provider_jobs SET status=?,lease_owner=NULL,
                           lease_token=NULL,lease_expires_at=NULL WHERE job_id=?""",
                        (status, self.job_id),
                    )
                    blocker = persistent_root_blocker(self.state._connection, topic_id=job.topic_id)
                assert blocker is not None
                self.assertEqual(blocker.kind, "control")
                self.assertEqual(blocker.cause_job_id, self.job_id)

    def test_job_completion_does_not_clear_control_owner_for_local_takeover(self) -> None:
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
                   interrupt_source='protective' WHERE job_id=?""",
                (datetime.now(timezone.utc).isoformat(), "example-control-owner", self.job_id),
            )
            self.state._connection.execute(
                """UPDATE provider_jobs SET status='completed',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL WHERE job_id=?""",
                (self.job_id,),
            )
        job = self.state.get_provider_job(self.job_id)
        with self.assertRaises(StateError):
            self.state.set_writer_mode(job.session_id, "local")

    def test_first_covering_stop_and_schedule_cannot_reset_on_another_stop(self) -> None:
        self.journal.record_turn(self.job_id, self.token, "example-turn")
        job = self.state.get_provider_job(self.job_id)
        first, _, _ = self.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=98, target_agent_id="codex"
        )
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE codex_turn_controls SET late_read_attempts=2 WHERE job_id=?", (self.job_id,)
            )
        before = dict(self.row())
        second, _, _ = self.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=99, target_agent_id="codex"
        )
        self.assertNotEqual(first, second)
        self.assertEqual(self.row()["stop_request_id"], first)
        self.assertEqual(dict(self.row()), before)


if __name__ == "__main__":
    unittest.main()
