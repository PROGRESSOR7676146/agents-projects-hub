from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import Mock

from hermes_codex_router.task_notice_sender import deliver_task_notice
from tests import test_task_activity as fixtures


class TaskActivitySenderGuardTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.TaskActivityTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.bind()
        return fixture

    def test_changed_binding_suppresses_first_send_without_touching_execution(self):
        changes = (
            "UPDATE provider_jobs SET lease_token='replacement'",
            "UPDATE provider_jobs SET lease_expires_at='2025-01-01T00:00:00+00:00'",
            "UPDATE agent_sessions SET generation=2",
            "UPDATE agent_sessions SET writer_mode='local'",
            "UPDATE agent_sessions SET provider_session_id='replacement'",
            "UPDATE topics SET thread_id=8",
            "UPDATE topics SET execution_scope='root:/home/example/other'",
            "UPDATE provider_execution_checkpoints SET provider_turn_id='replacement'",
            "UPDATE provider_execution_checkpoints SET completed_text='Finished'",
        )
        for kind in ("approval_wait", "no_progress"):
            for change in changes:
                with self.subTest(kind=kind, change=change):
                    f = self.fixture()
                    if kind == "approval_wait":
                        f.event("approval_requested", request=1)
                    else:
                        f.evaluate(300)
                    with f.transaction():
                        f.db.execute(change)
                    bot = Mock()
                    result = deliver_task_notice(
                        f.notices, bot, "sender", now=f.now + timedelta(seconds=301)
                    )
                    self.assertTrue(result.worked)
                    self.assertIsNone(result.error)
                    self.assertFalse(result.delivered)
                    bot.send_html.assert_not_called()
                    row = f.db.execute(
                        "SELECT status,attempt_count FROM task_lifecycle_notices"
                    ).fetchone()
                    self.assertEqual(tuple(row), ("superseded", 0))
                    self.assertEqual(
                        f.db.execute("SELECT status FROM provider_jobs").fetchone()[0], "executing"
                    )

    def test_changed_episode_or_resolved_approval_suppresses_first_send(self):
        for kind in ("approval_wait", "no_progress"):
            with self.subTest(kind=kind):
                f = self.fixture()
                if kind == "approval_wait":
                    f.event("approval_requested", request=1)
                    mutation = (
                        "UPDATE task_activity_entries SET state='resolved' WHERE kind='approval'"
                    )
                else:
                    f.evaluate(300)
                    mutation = "UPDATE task_activity SET episode=episode+1"
                with f.transaction():
                    f.db.execute(mutation)
                bot = Mock()
                result = deliver_task_notice(
                    f.notices, bot, "sender", now=f.now + timedelta(seconds=301)
                )
                self.assertTrue(result.worked)
                bot.send_html.assert_not_called()

    def test_completed_checkpoint_does_not_generate_new_no_progress_notice(self):
        f = self.fixture()
        with f.transaction():
            f.db.execute("UPDATE provider_execution_checkpoints SET completed_text='Finished'")
        self.assertEqual(f.evaluate(300), ())

    def test_attempted_rejection_remains_retryable_after_binding_changes(self):
        f = self.fixture()
        f.event("approval_requested", request=1)
        now = f.now + timedelta(seconds=2)
        lease = f.notices.lease_notice("sender", now=now)
        assert lease is not None and lease.lease_token is not None
        f.notices.begin_send(lease.notice_id, lease.lease_token, now=now)
        f.notices.retry_rejected(
            lease.notice_id,
            lease.lease_token,
            error_code="fictional_rejection",
            available_at=now,
            now=now,
        )
        with f.transaction():
            f.db.execute("UPDATE provider_jobs SET lease_token='replacement'")
        bot = Mock()
        bot.send_html.return_value = 23
        result = deliver_task_notice(f.notices, bot, "sender", now=now)
        self.assertTrue(result.delivered)
        retained = f.notices.get_notice(lease.notice_id)
        self.assertEqual((retained.status, retained.attempt_count), ("delivered", 2))

    def test_current_activity_notices_deliver_normally(self):
        for kind in ("approval_wait", "no_progress"):
            with self.subTest(kind=kind):
                f = self.fixture()
                if kind == "approval_wait":
                    f.event("approval_requested", request=1)
                else:
                    f.evaluate(300)
                bot = Mock()
                bot.send_html.return_value = 23
                result = deliver_task_notice(
                    f.notices, bot, "sender", now=f.now + timedelta(seconds=301)
                )
                self.assertTrue(result.delivered)
                bot.send_html.assert_called_once()
