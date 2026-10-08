"""Durable no-progress observations preserve invocation and human authority."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router.claude_activity import ClaudeActivityState
from hermes_codex_router.claude_activity_binding import claude_activity_notice_is_current
from hermes_codex_router.claude_permissions_journal import ClaudePermissionJournal
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.worker_claude_activity import claude_process_observation_for_turn
from tests import test_claude_invocation_journal as fixture_module


class ClaudeActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.ClaudeInvocationJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.db = self.state._connection
        self.job = self.fixture.job
        self.token = self.fixture.token
        self.native = self.fixture.binding.session_id
        self.now = datetime.now(timezone.utc)
        self.permission = ClaudePermissionJournal(self.state)
        self.permission.bind_session_mode(
            self.job.job_id,
            self.token,
            self.native,
            self.fixture.root,
            mode="text_only",
            home=self.fixture.base / "session",
            is_new=True,
        )
        self.mutate(
            "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
            ((self.now + timedelta(days=1)).isoformat(), self.job.job_id),
        )
        self.observer = ClaudeActivityState(
            self.db,
            transaction=self.state._immediate_transaction,
            state_error=StateError,
            notices=self.state.task_notices,
            notices_enabled=True,
        )

    def mutate(self, sql, values=()) -> None:
        with self.state._immediate_transaction():
            self.db.execute(sql, values)

    def open(self, seconds=0):
        return self.observer.open_process_observation(
            self.job.job_id,
            self.token,
            self.native,
            str(self.fixture.root),
            now=self.now + timedelta(seconds=seconds),
        )

    def evaluate(self, seconds):
        return self.observer.evaluate(now=self.now + timedelta(seconds=seconds))

    def row(self):
        return self.db.execute("SELECT * FROM claude_activity_observations").fetchone()

    def item(self, seconds):
        self.fixture.item(message_id=fixture_module.MESSAGE_UUID)
        self.mutate(
            "UPDATE provider_visible_items SET created_at=? WHERE job_id=?",
            ((self.now + timedelta(seconds=seconds)).isoformat(), self.job.job_id),
        )

    def begin(self, notice, seconds):
        now = self.now + timedelta(seconds=seconds)
        leased = self.state.task_notices.lease_notice("example-sender", now=now)
        self.assertIsNotNone(leased)
        assert leased is not None and leased.lease_token is not None
        self.assertEqual(leased.notice_id, notice.notice_id)
        return self.state.task_notices.begin_send(leased.notice_id, leased.lease_token, now=now)

    def file_tools(self):
        self.mutate("UPDATE claude_permission_session_modes SET mode='file_tools'")
        return self.permission.open_launch(
            self.job.job_id, self.token, self.native, self.fixture.root
        )

    def request(self, launch):
        payload = self.permission.prepare(
            launch,
            str(uuid.uuid4()),
            "a" * 64,
            "Write",
            {"file_path": "example.txt", "content": "Example"},
        )
        self.mutate(
            "UPDATE claude_permission_requests SET expires_at=?",
            (int((self.now + timedelta(days=1)).timestamp() * 1000),),
        )
        return payload

    def test_prepared_checkpoint_alone_creates_no_notice(self) -> None:
        self.assertEqual(self.evaluate(1000), ())
        self.assertIsNone(self.row())

    def test_process_start_is_idempotent_and_does_not_claim_native_turn(self) -> None:
        self.assertTrue(self.open())
        self.assertFalse(self.open(200))
        self.assertEqual(self.row()["quiet_since_at"], self.now.isoformat())
        checkpoint = self.fixture.journal.read(self.job.job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertEqual(self.evaluate(299), ())
        notices = self.evaluate(300)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].kind, "claude_no_progress")
        self.assertEqual(self.evaluate(900), ())

    def test_new_durable_visible_item_rearms_but_duplicate_does_not(self) -> None:
        self.open()
        first = self.evaluate(300)[0]
        self.item(301)
        self.assertEqual(self.evaluate(302), ())
        self.assertEqual(self.state.task_notices.get_notice(first.notice_id).status, "superseded")
        self.fixture.item(message_id=fixture_module.MESSAGE_UUID)
        self.assertEqual(self.evaluate(600), ())
        self.assertEqual(len(self.evaluate(601)), 1)

    def test_committed_message_between_evaluation_and_send_suppresses_warning(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]
        self.item(301)
        self.assertEqual(self.begin(notice, 302).status, "superseded")

    def test_retirement_clears_unattempted_sender_lease_across_connections(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]
        leased = self.state.task_notices.lease_notice(
            "example-sender", now=self.now + timedelta(seconds=301)
        )
        assert leased is not None and leased.lease_token is not None
        worker = HubState.open(self.fixture.path, codex_permission_profile=None)
        try:
            observer = ClaudeActivityState(
                worker._connection,
                transaction=worker._immediate_transaction,
                state_error=StateError,
                notices=worker.task_notices,
                notices_enabled=True,
            )
            observer.retire(self.job.job_id, self.token, now=self.now + timedelta(seconds=302))
        finally:
            worker.close()
        self.assert_superseded_lease(notice, leased)

    def test_visible_progress_clears_unattempted_sender_lease(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]
        leased = self.state.task_notices.lease_notice(
            "example-sender", now=self.now + timedelta(seconds=301)
        )
        assert leased is not None and leased.lease_token is not None
        self.item(302)
        self.assertEqual(self.evaluate(303), ())
        self.assert_superseded_lease(notice, leased)

    def assert_superseded_lease(self, notice, leased) -> None:
        kept = self.state.task_notices.get_notice(notice.notice_id)
        self.assertEqual((kept.status, kept.attempt_count), ("superseded", 0))
        self.assertIsNone(kept.send_started_at)
        self.assertEqual((kept.lease_token, kept.lease_owner, kept.lease_expires_at), (None,) * 3)
        stale = self.state.task_notices.begin_send(
            notice.notice_id, leased.lease_token, now=self.now + timedelta(seconds=304)
        )
        self.assertEqual(stale, kept)

    def test_binding_completion_and_lease_drift_cannot_retarget_notice(self) -> None:
        self.open()
        for statement, restore, values in (
            (
                "UPDATE agent_sessions SET generation=generation+1",
                "UPDATE agent_sessions SET generation=generation-1",
                (),
            ),
            (
                "UPDATE agent_sessions SET writer_mode='local'",
                "UPDATE agent_sessions SET writer_mode='telegram'",
                (),
            ),
            ("UPDATE topics SET thread_id=8", "UPDATE topics SET thread_id=7", ()),
            (
                "UPDATE topics SET project_id='other-example'",
                "UPDATE topics SET project_id='example-project'",
                (),
            ),
            (
                "UPDATE topics SET execution_scope='project:other-example'",
                "UPDATE topics SET execution_scope=?",
                (f"root:{self.fixture.root}",),
            ),
            (
                "UPDATE provider_jobs SET lease_token='other'",
                "UPDATE provider_jobs SET lease_token=?",
                (self.token,),
            ),
            (
                "UPDATE provider_execution_checkpoints SET completed_text='Done'",
                "UPDATE provider_execution_checkpoints SET completed_text=NULL",
                (),
            ),
            (
                "UPDATE provider_execution_checkpoints SET provider_turn_id='invented'",
                "UPDATE provider_execution_checkpoints SET provider_turn_id=NULL",
                (),
            ),
        ):
            with self.subTest(statement=statement):
                self.mutate(statement)
                self.assertEqual(self.evaluate(300), ())
                self.mutate(restore, values)

    def test_pending_permission_suppresses_notice_and_resolution_grace_runs_once(self) -> None:
        launch = self.file_tools()
        self.open()
        payload = self.request(launch)
        self.assertEqual(self.evaluate(500), ())
        self.assertEqual(self.evaluate(900), ())
        self.permission.consume(launch, payload, "deny")
        self.assertEqual(self.evaluate(1000), ())
        self.assertEqual(self.evaluate(1299), ())
        self.assertEqual(len(self.evaluate(1300)), 1)
        self.assertEqual(self.evaluate(1500), ())

    def test_unobserved_permission_roundtrip_between_evaluation_and_send_is_stale(self) -> None:
        launch = self.file_tools()
        self.open()
        notice = self.evaluate(300)[0]
        leased = self.state.task_notices.lease_notice(
            "example-sender", now=self.now + timedelta(seconds=300)
        )
        assert leased is not None and leased.lease_token is not None
        payload = self.request(launch)
        self.permission.consume(launch, payload, "deny")
        sent = self.state.task_notices.begin_send(
            leased.notice_id, leased.lease_token, now=self.now + timedelta(seconds=301)
        )
        self.assertEqual(sent.notice_id, notice.notice_id)
        self.assertEqual(sent.status, "superseded")
        self.assertEqual(self.evaluate(302), ())
        self.assertEqual(len(self.evaluate(602)), 1)

    def test_unavailable_permission_launch_cannot_be_treated_as_no_wait(self) -> None:
        self.mutate("UPDATE claude_permission_session_modes SET mode='file_tools'")
        with self.assertRaises(StateError):
            self.open()
        self.assertIsNone(self.row())

    def test_retirement_is_permanent_and_preserves_attempted_delivery(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]
        sent = self.begin(notice, 301)
        self.assertEqual(sent.attempt_count, 1)
        self.observer.retire(self.job.job_id, self.token, now=self.now + timedelta(seconds=302))
        self.assertFalse(self.open(303))
        self.assertEqual(self.evaluate(1000), ())
        kept = self.state.task_notices.get_notice(notice.notice_id)
        self.assertEqual(kept.status, "leased")
        self.assertEqual(kept.send_started_at, sent.send_started_at)

    def test_notice_and_episode_rollback_together_when_preparation_fails(self) -> None:
        self.open()
        before = tuple(self.row())
        with patch.object(
            self.state.task_notices,
            "prepare_notice_in_transaction",
            side_effect=StateError("fictional"),
        ):
            with self.assertRaises(StateError):
                self.evaluate(300)
        self.assertEqual(tuple(self.row()), before)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM task_lifecycle_notices").fetchone()[0], 0
        )
        self.assertEqual(len(self.evaluate(300)), 1)

    def test_pending_permission_after_evaluation_blocks_first_send(self) -> None:
        launch = self.file_tools()
        self.open()
        notice = self.evaluate(300)[0]
        leased = self.state.task_notices.lease_notice(
            "example-sender", now=self.now + timedelta(seconds=300)
        )
        assert leased is not None and leased.lease_token is not None
        self.request(launch)
        sent = self.state.task_notices.begin_send(
            leased.notice_id, leased.lease_token, now=self.now + timedelta(seconds=301)
        )
        self.assertEqual(
            (sent.notice_id, sent.status, sent.attempt_count), (notice.notice_id, "superseded", 0)
        )

    def test_permission_expiry_starts_one_quiet_grace_interval(self) -> None:
        launch = self.file_tools()
        self.open()
        self.request(launch)
        self.mutate(
            "UPDATE claude_permission_requests SET expires_at=?",
            (int((self.now + timedelta(seconds=600)).timestamp() * 1000),),
        )
        self.assertEqual(self.evaluate(500), ())
        self.assertEqual(self.evaluate(600), ())
        self.assertEqual(self.evaluate(899), ())
        self.assertEqual(len(self.evaluate(900)), 1)
        self.assertEqual(self.evaluate(1200), ())

    def test_permission_revocation_starts_one_quiet_grace_interval(self) -> None:
        launch = self.file_tools()
        self.open()
        self.request(launch)
        self.assertEqual(self.evaluate(500), ())
        nonce = self.db.execute("SELECT request_nonce FROM claude_permission_requests").fetchone()[
            0
        ]
        self.permission.revoke_request(launch, nonce)
        self.assertEqual(self.evaluate(600), ())
        self.assertEqual(self.evaluate(899), ())
        self.assertEqual(len(self.evaluate(900)), 1)
        self.assertEqual(self.evaluate(1200), ())

    def test_closed_launch_permanently_retires_observation_without_replay(self) -> None:
        launch = self.file_tools()
        self.open()
        notice = self.evaluate(300)[0]
        self.permission.close_launch(launch)
        self.assertEqual(self.evaluate(301), ())
        self.assertIsNotNone(self.row()["retired_at"])
        self.assertEqual(self.state.task_notices.get_notice(notice.notice_id).status, "superseded")
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "executing")

    def test_changed_permission_digest_fails_closed_and_retires(self) -> None:
        self.file_tools()
        self.open()
        notice = self.evaluate(300)[0]
        self.mutate("UPDATE claude_permission_launches SET binding_digest=?", ("b" * 64,))
        self.assertEqual(self.begin(notice, 301).status, "superseded")
        self.assertEqual(self.evaluate(302), ())
        self.assertIsNotNone(self.row()["retired_at"])

    def test_permission_snapshot_accepts_128_and_refuses_129_rows(self) -> None:
        launch = self.file_tools()
        self.open()
        with self.state._immediate_transaction():
            for index in range(128):
                self.db.execute(
                    "INSERT INTO claude_permission_requests "
                    "(request_nonce,launch_epoch,payload_digest,event_digest,status,expires_at) "
                    "VALUES (?,?,?,?,'deny',?)",
                    (f"example-{index}", launch.epoch, "a" * 64, "b" * 64, 1),
                )
        self.assertEqual(self.evaluate(301), ())
        notice = self.evaluate(601)[0]
        self.mutate(
            "INSERT INTO claude_permission_requests "
            "(request_nonce,launch_epoch,payload_digest,event_digest,status,expires_at) "
            "VALUES ('example-extra',?,?,?,'deny',1)",
            (launch.epoch, "a" * 64, "b" * 64),
        )
        self.assertEqual(self.begin(notice, 602).status, "superseded")
        self.assertEqual(self.evaluate(603), ())
        self.assertIsNotNone(self.row()["retired_at"])

    def test_first_send_rechecks_immutable_bindings_and_covering_stop(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]

        def current():
            return claude_activity_notice_is_current(
                self.db,
                job_id=self.job.job_id,
                event_key=notice.event_key,
                chat_id=notice.chat_id,
                thread_id=notice.thread_id,
                created_at=notice.created_at,
                timestamp=(self.now + timedelta(seconds=301)).isoformat(),
            )

        self.assertTrue(current())
        for table, column, replacement in (
            ("provider_jobs", "lease_token", "wrong-token"),
            ("provider_jobs", "lease_expires_at", self.now.isoformat()),
            ("provider_jobs", "status", "leased"),
            ("provider_jobs", "chat_id", -1002222222222),
            ("provider_jobs", "provider_started_at", (self.now - timedelta(seconds=1)).isoformat()),
            ("topics", "thread_id", 8),
            ("topics", "project_id", "other-example"),
            ("topics", "execution_scope", "project:other-example"),
            ("agent_sessions", "writer_mode", "local"),
            ("agent_sessions", "generation", self.job.session_generation + 1),
            ("agent_sessions", "status", "archived"),
            ("agent_sessions", "agent_id", "other-agent"),
            ("agent_sessions", "provider_session_id", fixture_module.OTHER_UUID),
            ("provider_execution_checkpoints", "provider_thread_id", fixture_module.OTHER_UUID),
            ("provider_execution_checkpoints", "project_root", str(self.fixture.other_root)),
            ("provider_execution_checkpoints", "provider_turn_id", "invented-turn"),
            ("provider_execution_checkpoints", "completed_text", "Done"),
        ):
            with self.subTest(table=table, column=column):
                original = self.db.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                self.mutate(f"UPDATE {table} SET {column}=?", (replacement,))
                self.assertFalse(current())
                self.mutate(f"UPDATE {table} SET {column}=?", (original,))
                self.assertTrue(current())
        self.state.request_emergency_stop(
            topic_id=self.job.topic_id,
            chat_id=self.job.chat_id,
            message_id=10,
            target_agent_id=self.job.agent_id,
        )
        self.assertEqual(self.begin(notice, 301).status, "superseded")

    def test_stale_retirement_token_preserves_current_observation(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]
        self.observer.retire(self.job.job_id, "stale", now=self.now + timedelta(seconds=301))
        self.assertIsNone(self.row()["retired_at"])
        self.assertEqual(self.begin(notice, 302).attempt_count, 1)

    def test_unknown_send_is_preserved_and_not_released_by_retirement(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]
        sent = self.begin(notice, 301)
        assert sent.lease_token is not None
        self.state.task_notices.mark_send_unknown(
            sent.notice_id,
            sent.lease_token,
            error_code="example_timeout",
            now=self.now + timedelta(seconds=302),
        )
        self.observer.retire(self.job.job_id, self.token, now=self.now + timedelta(seconds=303))
        self.assertEqual(self.state.task_notices.get_notice(notice.notice_id).status, "unknown")
        self.assertIsNone(
            self.state.task_notices.lease_notice(
                "example-sender", now=self.now + timedelta(seconds=1000)
            )
        )

    def test_known_rejection_retains_copy_and_retry_deadline_after_retirement(self) -> None:
        self.open()
        notice = self.evaluate(300)[0]
        sent = self.begin(notice, 301)
        assert sent.lease_token is not None
        due = self.now + timedelta(seconds=400)
        self.state.task_notices.retry_rejected(
            sent.notice_id,
            sent.lease_token,
            error_code="example_rejected",
            available_at=due,
            now=self.now + timedelta(seconds=302),
        )
        self.observer.retire(self.job.job_id, self.token, now=self.now + timedelta(seconds=303))
        self.assertIsNone(
            self.state.task_notices.lease_notice("example-sender", now=due - timedelta(seconds=1))
        )
        retried = self.begin(notice, 400)
        self.assertEqual(
            (retried.status, retried.attempt_count, retried.telegram_html),
            ("leased", 2, notice.telegram_html),
        )

    def test_reopened_state_uses_persisted_episode_without_fabricating_new_start(self) -> None:
        self.open()
        first = self.evaluate(300)[0]
        reopened = HubState.open(self.fixture.path, codex_permission_profile=None)
        self.addCleanup(reopened.close)
        observer = ClaudeActivityState(
            reopened._connection,
            transaction=reopened._immediate_transaction,
            state_error=StateError,
            notices=reopened.task_notices,
            notices_enabled=True,
        )
        self.assertEqual(observer.evaluate(now=self.now + timedelta(seconds=600)), ())
        self.assertEqual(reopened.task_notices.get_notice(first.notice_id), first)
        self.assertEqual(self.row()["process_observed_at"], self.now.isoformat())
        self.item(601)
        self.assertEqual(observer.evaluate(now=self.now + timedelta(seconds=602)), ())
        self.assertEqual(len(observer.evaluate(now=self.now + timedelta(seconds=901))), 1)

    def test_context_retirement_blocks_late_process_callback(self) -> None:
        with claude_process_observation_for_turn(
            self.state,
            self.job.job_id,
            self.token,
            self.fixture.binding,
            str(self.fixture.root),
            enabled=True,
            clock=lambda: self.now,
        ) as callback:
            assert callback is not None
            callback()
            self.assertIsNone(self.row()["retired_at"])
        before = tuple(self.row())
        callback()
        self.assertEqual(tuple(self.row()), before)
        self.assertIsNotNone(self.row()["retired_at"])
