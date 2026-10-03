from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from hermes_codex_router.schema_task_lifecycle import TASK_LIFECYCLE_SCHEMA
from hermes_codex_router.task_lifecycle import TaskLifecycleState


class TaskLifecycleFixture(unittest.TestCase):
    now: datetime

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            "CREATE TABLE topics(topic_id INTEGER PRIMARY KEY, chat_id INTEGER, thread_id INTEGER);"
            "CREATE TABLE provider_jobs(job_id TEXT PRIMARY KEY, topic_id INTEGER, "
            "chat_id INTEGER, status TEXT);"
            "CREATE TABLE provider_stop_requests(request_id TEXT PRIMARY KEY, "
            "topic_id INTEGER, chat_id INTEGER, status TEXT);"
            "INSERT INTO topics VALUES(1,-1001234567890,7),(2,-1001234567890,8);"
            "INSERT INTO provider_jobs VALUES('job-a',1,-1001234567890,'executing');"
            "INSERT INTO provider_stop_requests VALUES('stop-a',1,-1001234567890,'pending');"
            "INSERT INTO provider_stop_requests VALUES('stop-b',2,-1001234567890,'pending');"
        )
        self.db.executescript(TASK_LIFECYCLE_SCHEMA)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.state = TaskLifecycleState(
            self.db, transaction=self.transaction, state_error=ValueError
        )

    def tearDown(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def prepare(self, **overrides: Any):
        values: dict[str, Any] = dict(
            event_key="stop:stop-a:requested",
            kind="stop_requested",
            stop_request_id="stop-a",
            chat_id=-1001234567890,
            thread_id=7,
            reply_to_message_id=10,
            telegram_html="Stop requested; interruption is not yet confirmed.",
            now=self.now,
        )
        values.update(overrides)
        with self.transaction():
            return self.state.prepare_notice_in_transaction(**values)

    def test_transient_notice_guard_covers_terminal_states_and_activity_kinds(self) -> None:
        for status in ("completed", "failed", "cancelled", "indeterminate", "result_ready"):
            for kind in ("accepted", "queued", "executing", "approval_wait", "no_progress"):
                with self.subTest(status=status, kind=kind):
                    notice, _ = self.prepare(
                        event_key=f"job:job-a:{status}:{kind}",
                        kind=kind,
                        job_id="job-a",
                        stop_request_id=None,
                    )
                    leased = self.state.lease_notice("sender-a", now=self.now)
                    assert leased is not None and leased.lease_token is not None
                    with self.transaction():
                        self.db.execute("UPDATE provider_jobs SET status=?", (status,))
                    result = self.state.begin_send(
                        notice.notice_id, leased.lease_token, now=self.now
                    )
                    self.assertEqual((result.status, result.attempt_count), ("superseded", 0))
                    self.assertIsNone(result.send_started_at)
                    self.assertIsNone(result.lease_token)

    def test_stop_notice_is_prioritized_over_older_queue_notices(self) -> None:
        self.prepare(
            event_key="job:job-a:queued",
            kind="queued",
            job_id="job-a",
            stop_request_id=None,
            now=self.now - timedelta(seconds=10),
        )
        stop, _ = self.prepare()
        leased = self.state.lease_notice("sender-a", now=self.now)
        assert leased is not None
        self.assertEqual(leased.notice_id, stop.notice_id)

    def test_terminal_guard_preserves_attempted_rejection_history(self) -> None:
        notice, _ = self.prepare(
            event_key="job:job-a:executing", kind="executing", job_id="job-a", stop_request_id=None
        )
        leased = self.state.lease_notice("sender-a", now=self.now)
        assert leased is not None and leased.lease_token is not None
        self.state.begin_send(notice.notice_id, leased.lease_token, now=self.now)
        self.state.retry_rejected(
            notice.notice_id,
            leased.lease_token,
            error_code="fictional_rejection",
            available_at=self.now,
            now=self.now,
        )
        with self.transaction():
            self.db.execute("UPDATE provider_jobs SET status='completed'")
        retry = self.state.lease_notice("sender-b", now=self.now)
        assert retry is not None and retry.lease_token is not None
        result = self.state.begin_send(notice.notice_id, retry.lease_token, now=self.now)
        self.assertEqual((result.status, result.attempt_count), ("leased", 2))
        self.assertEqual(result.error_code, "fictional_rejection")

    def started(self):
        notice, _ = self.prepare()
        leased = self.state.lease_notice("sender-a", now=self.now)
        assert leased is not None and leased.lease_token is not None
        self.state.begin_send(leased.notice_id, leased.lease_token, now=self.now)
        return self.state.get_notice(notice.notice_id), leased.lease_token


class TaskLifecycleTests(TaskLifecycleFixture):
    def test_notice_preparation_requires_callers_transaction(self) -> None:
        with self.assertRaises(ValueError):
            self.state.prepare_notice_in_transaction(
                event_key="event",
                kind="stop_requested",
                stop_request_id="stop-a",
                chat_id=-1001234567890,
                thread_id=7,
                telegram_html="Stop",
                now=self.now,
            )

    def test_stop_and_notice_rollback_together(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.transaction():
                self.db.execute("UPDATE provider_stop_requests SET status='completed'")
                self.state.prepare_notice_in_transaction(
                    event_key="event",
                    kind="stop_requested",
                    stop_request_id="stop-a",
                    chat_id=-1001234567890,
                    thread_id=7,
                    telegram_html="Stop",
                    now=self.now,
                )
                raise RuntimeError("fault before commit")
        self.assertEqual(self.state.notices_for_stop("stop-a"), ())
        self.assertEqual(
            self.db.execute("SELECT status FROM provider_stop_requests").fetchone()[0], "pending"
        )

    def test_duplicate_preparation_retains_identity_and_exact_content(self) -> None:
        first, created = self.prepare()
        second, duplicate = self.prepare(now=self.now + timedelta(seconds=1))
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first.notice_id, second.notice_id)
        with self.assertRaises(ValueError):
            self.prepare(telegram_html="A different stop outcome")

    def test_destination_must_match_every_bound_subject(self) -> None:
        for change in (
            {"thread_id": 8},
            {"chat_id": -1001111111111},
            {"stop_request_id": "missing"},
            {"stop_request_id": "stop-b", "job_id": "job-a", "thread_id": 8},
            {"stop_request_id": None},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(**change)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM task_lifecycle_notices").fetchone()[0], 0
        )

    def test_notice_bounds_and_numeric_identity_reject_invalid_values(self) -> None:
        for change in (
            {"telegram_html": "x" * 3501},
            {"telegram_html": " "},
            {"chat_id": True},
            {"thread_id": True},
            {"reply_to_message_id": True},
            {"reply_to_message_id": 0},
            {"now": datetime(2026, 1, 1)},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(**change)

    def test_lease_does_not_count_as_a_send_attempt_and_is_exclusive(self) -> None:
        notice, _ = self.prepare()
        lease = self.state.lease_notice("sender-a", now=self.now)
        assert lease is not None
        self.assertEqual(lease.attempt_count, 0)
        self.assertIsNone(self.state.lease_notice("sender-b", now=self.now))
        self.assertEqual(self.state.get_notice(notice.notice_id).status, "leased")

    def test_unattempted_expired_lease_returns_to_pending_without_spending_attempt(self) -> None:
        self.prepare()
        self.state.lease_notice("sender-a", now=self.now, lease_seconds=1)
        self.assertEqual(self.state.recover_expired_notices(now=self.now + timedelta(seconds=2)), 1)
        next_lease = self.state.lease_notice("sender-b", now=self.now + timedelta(seconds=2))
        assert next_lease is not None
        self.assertEqual(next_lease.attempt_count, 0)
        self.assertIsNone(next_lease.send_started_at)

    def test_expired_attempt_is_unknown_and_never_released_for_resend(self) -> None:
        notice, token = self.started()
        later = self.now + timedelta(seconds=100)
        self.assertEqual(self.state.recover_expired_notices(now=later), 1)
        retained = self.state.get_notice(notice.notice_id)
        self.assertEqual((retained.status, retained.attempt_count), ("unknown", 1))
        self.assertIsNone(self.state.lease_notice("sender-b", now=later))
        with self.assertRaises(ValueError):
            self.state.begin_send(notice.notice_id, token, now=later)

    def test_begin_send_cannot_be_repeated_with_same_lease(self) -> None:
        notice, token = self.started()
        with self.assertRaises(ValueError):
            self.state.begin_send(notice.notice_id, token, now=self.now)
        self.assertEqual(self.state.get_notice(notice.notice_id).attempt_count, 1)

    def test_positive_receipt_is_required_and_does_not_complete_provider_work(self) -> None:
        notice, token = self.started()
        for receipt in (True, False, 0, -1):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                self.state.complete_send(
                    notice.notice_id, token, telegram_message_id=receipt, now=self.now
                )
        delivered = self.state.complete_send(
            notice.notice_id, token, telegram_message_id=23, now=self.now
        )
        self.assertEqual((delivered.status, delivered.telegram_message_id), ("delivered", 23))
        self.assertEqual(
            self.db.execute("SELECT status FROM provider_jobs").fetchone()[0], "executing"
        )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM provider_stop_requests WHERE request_id='stop-a'"
            ).fetchone()[0],
            "pending",
        )

    def test_explicit_rejection_retains_deadline_across_facade_restart(self) -> None:
        notice, token = self.started()
        due = self.now + timedelta(seconds=60)
        pending = self.state.retry_rejected(
            notice.notice_id, token, error_code="api_rejection", available_at=due, now=self.now
        )
        self.assertEqual(pending.status, "pending")
        restarted = TaskLifecycleState(
            self.db, transaction=self.transaction, state_error=ValueError
        )
        self.assertIsNone(restarted.lease_notice("sender-b", now=due - timedelta(seconds=1)))
        self.assertEqual(restarted.get_notice(notice.notice_id).attempt_count, 1)
        self.assertIsNotNone(restarted.lease_notice("sender-b", now=due))

    def test_explicit_rejections_stop_after_bounded_attempts(self) -> None:
        self.prepare()
        for attempt in range(1, 4):
            lease = self.state.lease_notice("sender-a", now=self.now)
            assert lease is not None and lease.lease_token is not None
            self.state.begin_send(lease.notice_id, lease.lease_token, now=self.now)
            result = self.state.retry_rejected(
                lease.notice_id,
                lease.lease_token,
                error_code="api_rejection",
                available_at=self.now,
                now=self.now,
                max_attempts=3,
            )
            self.assertEqual(result.status, "failed" if attempt == 3 else "pending")
        self.assertIsNone(self.state.lease_notice("sender-a", now=self.now))

    def test_stale_or_unstarted_send_cannot_claim_receipt_or_retry(self) -> None:
        self.prepare()
        lease = self.state.lease_notice("sender-a", now=self.now)
        assert lease is not None and lease.lease_token is not None
        for token in (lease.lease_token, "wrong-token"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.state.complete_send(
                    lease.notice_id, token, telegram_message_id=23, now=self.now
                )

    def test_unknown_delivery_does_not_change_execution_or_stop(self) -> None:
        notice, token = self.started()
        self.state.mark_send_unknown(
            notice.notice_id, token, error_code="network_timeout", now=self.now
        )
        self.assertIsNone(self.state.lease_notice("sender-a", now=self.now))
        self.assertEqual(
            self.db.execute("SELECT status FROM provider_jobs").fetchone()[0], "executing"
        )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM provider_stop_requests WHERE request_id='stop-a'"
            ).fetchone()[0],
            "pending",
        )

    def test_job_only_notice_uses_its_numeric_topic(self) -> None:
        notice, created = self.prepare(stop_request_id=None, job_id="job-a")
        self.assertTrue(created)
        self.assertIsNone(notice.stop_request_id)
        self.assertEqual(notice.job_id, "job-a")

    def test_wrong_token_cannot_change_an_attempt_to_unknown(self) -> None:
        notice, _ = self.started()
        with self.assertRaises(ValueError):
            self.state.mark_send_unknown(
                notice.notice_id, "other-lease", error_code="timeout", now=self.now
            )
        self.assertEqual(self.state.get_notice(notice.notice_id).status, "leased")

    def test_expired_receipt_commit_is_not_accepted_as_delivery(self) -> None:
        notice, token = self.started()
        later = self.now + timedelta(seconds=100)
        with self.assertRaises(ValueError):
            self.state.complete_send(notice.notice_id, token, telegram_message_id=23, now=later)
        self.state.mark_send_unknown(notice.notice_id, token, error_code="late_receipt", now=later)
        retained = self.state.get_notice(notice.notice_id)
        self.assertEqual(retained.status, "unknown")
        self.assertIsNone(retained.telegram_message_id)

    def test_retry_rejects_past_deadline_without_losing_current_attempt(self) -> None:
        notice, token = self.started()
        with self.assertRaises(ValueError):
            self.state.retry_rejected(
                notice.notice_id,
                token,
                error_code="rejection",
                available_at=self.now - timedelta(seconds=1),
                now=self.now,
            )
        self.assertEqual(self.state.get_notice(notice.notice_id).status, "leased")
