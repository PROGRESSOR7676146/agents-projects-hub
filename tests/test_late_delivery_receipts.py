"""Delayed HTTP receipts and local commit faults, using real SQLite and no network."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from hermes_codex_router.final_delivery import deliver_final_part
from hermes_codex_router.progress_delivery_sender import deliver_progress
from hermes_codex_router.state import HubState, StateError
from tests import test_outbox_sender as fixtures
from tests.test_delivery_certainty import ReceiptBot


class LateDeliveryReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job_id = self.fixture.ready_outbox("opencode", 101)
        self.state = HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.now = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.later = self.now + timedelta(minutes=4)

    def fence(self):
        outbox = self.state.lease_telegram_outbox("opencode", "example-sender", now=self.now)
        assert outbox is not None and outbox.lease_token is not None
        self.state.delivery.begin_outbox_send(outbox.outbox_id, outbox.lease_token, 1, now=self.now)
        return outbox, outbox.lease_token

    def test_four_minute_positive_response_is_committed_without_second_http(self) -> None:
        outbox = self.state.lease_telegram_outbox("opencode", "example-sender", now=self.now)
        assert outbox is not None
        bot = ReceiptBot()
        times = iter((self.now, self.now, self.later))
        result = deliver_final_part(
            self.state.delivery,
            bot,
            outbox,
            state_path=self.fixture.config.state_path,
            clock=lambda: next(times),
        )
        self.assertTrue(result.receipt_committed)
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "completed")
        self.assertEqual(len(bot.sent), 1)

    def test_second_connection_stale_recovery_does_not_discard_exact_receipt(self) -> None:
        outbox, token = self.fence()
        with closing(HubState.open_existing(self.fixture.config.state_path)) as other:
            other.recover_stale_telegram_outbox(now=self.later)
            other.delivery.mark_outbox_delivered(
                outbox.outbox_id,
                token,
                part_index=1,
                telegram_message_id=71,
                now=self.later,
            )
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job_id).status, "delivered")

    def test_receipt_is_idempotent_but_conflicting_id_is_refused(self) -> None:
        outbox, token = self.fence()
        for _ in range(2):
            self.state.delivery.mark_outbox_delivered(
                outbox.outbox_id,
                token,
                part_index=1,
                telegram_message_id=71,
                now=self.later,
            )
        with self.assertRaises(StateError):
            self.state.delivery.mark_outbox_delivered(
                outbox.outbox_id,
                token,
                part_index=1,
                telegram_message_id=72,
                now=self.later,
            )
        self.assertEqual(
            self.state.get_telegram_outbox_parts(outbox.outbox_id)[0].telegram_message_id, 71
        )

    def test_changed_destination_refuses_receipt_projection(self) -> None:
        outbox, token = self.fence()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox SET thread_id=999 WHERE outbox_id=?",
                (outbox.outbox_id,),
            )
        self.state.delivery.mark_outbox_delivered(
            outbox.outbox_id,
            token,
            part_index=1,
            telegram_message_id=71,
            now=self.later,
        )
        self.assertIsNone(
            self.state.get_telegram_outbox_parts(outbox.outbox_id)[0].telegram_message_id
        )

    def test_owner_disposition_is_preserved_when_receipt_arrives_late(self) -> None:
        outbox, token = self.fence()
        self.state.recover_stale_telegram_outbox(now=self.later)
        self.state.reconcile_legacy_execution_scopes({"example-project": self.fixture.base})
        preview = self.state.preview_delivery_hold(outbox.outbox_id)
        self.state.release_delivery_hold(
            outbox.outbox_id,
            expected_snapshot=preview.snapshot,
            continue_without_confirmed_delivery=True,
            now=self.later,
        )
        before = self.state.get_telegram_outbox_for_job(self.job_id)
        self.state.delivery.mark_outbox_delivered(
            outbox.outbox_id,
            token,
            part_index=1,
            telegram_message_id=71,
            now=self.later,
        )
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job_id), before)
        receipt = self.state._connection.execute(
            "SELECT telegram_message_id FROM telegram_send_receipts WHERE attempt_id=?",
            (token,),
        ).fetchone()
        self.assertEqual(receipt[0], 71)
        self.assertEqual(
            self.state.preview_delivery_hold(outbox.outbox_id).hold_status, "released_by_owner"
        )

    def test_transient_local_receipt_fault_retries_sql_only(self) -> None:
        outbox = self.state.lease_telegram_outbox("opencode", "example-sender", now=self.now)
        assert outbox is not None
        bot = ReceiptBot()
        original = self.state.delivery.mark_outbox_delivered
        calls = 0

        def commit(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        with patch.object(self.state.delivery, "mark_outbox_delivered", side_effect=commit):
            result = deliver_final_part(
                self.state.delivery,
                bot,
                outbox,
                state_path=self.fixture.config.state_path,
                now=self.now,
            )
        self.assertTrue(result.receipt_committed)
        self.assertEqual(calls, 2)
        self.assertEqual(len(bot.sent), 1)

    def test_restart_projects_durable_receipt_after_exhausted_bookkeeping_fault(self) -> None:
        outbox = self.state.lease_telegram_outbox("opencode", "example-sender", now=self.now)
        assert outbox is not None
        bot = ReceiptBot()
        with patch.object(
            self.state.delivery._receipts, "settle", side_effect=OSError("disk")
        ) as settle:
            result = deliver_final_part(
                self.state.delivery,
                bot,
                outbox,
                state_path=self.fixture.config.state_path,
                now=self.now,
            )
            self.assertEqual(settle.call_count, 3)
        self.assertFalse(result.receipt_committed)
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job_id).status, "unknown")
        with closing(HubState.open_existing(self.fixture.config.state_path)) as restarted:
            restarted.recover_stale_telegram_outbox(now=self.later)
            self.assertEqual(restarted.get_provider_job(self.job_id).status, "completed")
        self.assertEqual(len(bot.sent), 1)

    def test_failure_before_durable_receipt_remains_unknown_and_bounded(self) -> None:
        outbox = self.state.lease_telegram_outbox("opencode", "example-sender", now=self.now)
        assert outbox is not None
        bot = ReceiptBot()
        with patch.object(
            self.state.delivery._receipts, "record", side_effect=sqlite3.OperationalError("disk")
        ) as record:
            result = deliver_final_part(
                self.state.delivery,
                bot,
                outbox,
                state_path=self.fixture.config.state_path,
                now=self.now,
            )
            self.assertEqual(record.call_count, 3)
        self.assertFalse(result.receipt_committed)
        self.state.recover_stale_telegram_outbox(now=self.later)
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job_id).status, "unknown")
        self.assertIsNone(
            self.state.lease_telegram_outbox("opencode", "example-next", now=self.later)
        )
        self.assertEqual(len(bot.sent), 1)

    def test_receipt_change_invalidates_unapplied_owner_preview(self) -> None:
        outbox, token = self.fence()
        self.state.recover_stale_telegram_outbox(now=self.later)
        self.state.reconcile_legacy_execution_scopes({"example-project": self.fixture.base})
        preview = self.state.preview_delivery_hold(outbox.outbox_id)
        with self.state._immediate_transaction():
            self.state.delivery._receipts.record(
                "final", outbox.outbox_id, token, 1, 71, self.later.isoformat()
            )
        with self.assertRaises(StateError):
            self.state.release_delivery_hold(
                outbox.outbox_id,
                expected_snapshot=preview.snapshot,
                continue_without_confirmed_delivery=True,
            )

    def test_new_attempt_ownership_cannot_be_changed_by_old_receipt(self) -> None:
        outbox, token = self.fence()
        self.state.delivery.retry_outbox(
            outbox.outbox_id, token, error_code="rejected", delay_seconds=0, now=self.now
        )
        second = self.state.lease_telegram_outbox("opencode", "example-new", now=self.now)
        assert second and second.lease_token
        self.state.delivery.begin_outbox_send(outbox.outbox_id, second.lease_token, 1, now=self.now)
        before = self.state.get_telegram_outbox_for_job(self.job_id)
        self.state.delivery.mark_outbox_delivered(
            outbox.outbox_id, token, part_index=1, telegram_message_id=71, now=self.later
        )
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job_id), before)
        self.assertIsNone(
            self.state.get_telegram_outbox_parts(outbox.outbox_id)[0].telegram_message_id
        )

    def test_attempt_and_receipt_evidence_refuse_update_delete_and_replace(self) -> None:
        outbox, token = self.fence()
        self.state.delivery.mark_outbox_delivered(
            outbox.outbox_id, token, part_index=1, telegram_message_id=71, now=self.now
        )
        for sql in (
            "DELETE FROM telegram_send_attempts",
            "DELETE FROM telegram_send_receipts",
            "UPDATE telegram_send_attempts SET binding_hash=binding_hash",
            "UPDATE telegram_send_receipts SET telegram_message_id=72",
            "INSERT OR REPLACE INTO telegram_send_attempts SELECT * FROM telegram_send_attempts",
            "INSERT OR REPLACE INTO telegram_send_receipts SELECT * FROM telegram_send_receipts",
            "UPDATE telegram_outbox SET purpose='execution_notice'",
        ):
            with (
                self.subTest(sql=sql),
                self.assertRaises(sqlite3.IntegrityError),
                self.state._connection,
            ):
                self.state._connection.execute(sql)

    def test_receipt_cannot_be_replaced_through_a_different_attempt_rowid(self) -> None:
        outbox, token = self.fence()
        self.state.delivery.mark_outbox_delivered(
            outbox.outbox_id, token, part_index=1, telegram_message_id=71, now=self.now
        )
        with self.state._immediate_transaction():
            self.state.delivery._receipts.begin(
                "final", outbox.outbox_id, "example-other-attempt", 1, self.now.isoformat()
            )
        with self.assertRaises(sqlite3.Error), self.state._connection:
            self.state._connection.execute(
                """INSERT OR REPLACE INTO telegram_send_receipts
                   (rowid,attempt_id,telegram_message_id,recorded_at)
                   VALUES(1,'example-other-attempt',72,?)""",
                (self.now.isoformat(),),
            )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT attempt_id,telegram_message_id FROM telegram_send_receipts"
            ).fetchall()[0][:],
            (token, 71),
        )

    def test_archival_receipts_cannot_starve_restart_reconciliation(self) -> None:
        outbox = self.state.get_telegram_outbox_for_job(self.job_id)
        assert outbox
        with self.state._immediate_transaction():
            for index in range(64):
                token = f"example-archival-{index}"
                self.state.delivery._receipts.begin(
                    "final", outbox.outbox_id, token, 1, self.now.isoformat()
                )
                self.state.delivery._receipts.record(
                    "final", outbox.outbox_id, token, 1, index + 1, self.now.isoformat()
                )
        outbox, token = self.fence()
        with self.state._immediate_transaction():
            self.state.delivery._receipts.record(
                "final", outbox.outbox_id, token, 1, 71, self.later.isoformat()
            )
        self.state.recover_stale_telegram_outbox(now=self.later)
        self.state.recover_stale_telegram_outbox(now=self.later)
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "completed")

    def test_payload_change_between_read_and_fence_causes_no_http(self) -> None:
        outbox = self.state.lease_telegram_outbox("opencode", "example-sender", now=self.now)
        assert outbox
        bot = ReceiptBot()
        original = self.state.delivery.next_outbox_part
        changed = False

        def read_then_change(*args: Any, **kwargs: Any):
            nonlocal changed
            part = original(*args, **kwargs)
            if not changed:
                changed = True
                with self.state._connection:
                    self.state._connection.execute(
                        "UPDATE telegram_outbox_parts SET telegram_html='Changed' WHERE outbox_id=?",
                        (outbox.outbox_id,),
                    )
            return part

        with patch.object(self.state.delivery, "next_outbox_part", side_effect=read_then_change):
            result = deliver_final_part(
                self.state.delivery,
                bot,
                outbox,
                state_path=self.fixture.config.state_path,
                now=self.now,
            )
        self.assertIsInstance(result.error, StateError)
        self.assertEqual(bot.sent, [])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_send_attempts"
            ).fetchone()[0],
            0,
        )


class NoticeDeliveryOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.state = HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        topic = self.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
        )
        self.session = self.state.activate_agent(topic.topic_id, "opencode", "model-1", "high")
        self.topic_id = topic.topic_id

    def enqueue(self, message_id: int):
        return self.state.enqueue_provider_job(
            idempotency_key=f"example:{message_id}",
            chat_id=-1001234567890,
            message_id=message_id,
            topic_id=self.topic_id,
            agent_id="opencode",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            provider_session_id=None,
            model="model-1",
            effort="high",
            payload_text="Example",
            context_watermark=None,
            handoff_id=None,
        )[0]

    def test_unknown_new_execution_notice_allows_later_saved_result(self) -> None:
        first = self.enqueue(1)
        leased = self.state.lease_provider_job("opencode", "example-worker")
        assert leased and leased.lease_token
        self.state.mark_provider_job_executing(first.job_id, leased.lease_token)
        self.state.terminate_provider_job_with_notice(
            first.job_id,
            leased.lease_token,
            status="failed",
            error_class="pre_execution",
            error_code="example",
            sender_agent_id="opencode",
            telegram_html="Example notice",
        )
        second = self.enqueue(2)
        leased = self.state.lease_provider_job("opencode", "example-worker")
        assert leased and leased.lease_token and leased.job_id == second.job_id
        self.state.mark_provider_job_executing(second.job_id, leased.lease_token)
        self.state.commit_provider_result(
            second.job_id,
            leased.lease_token,
            visible_response="Example result",
            sender_agent_id="opencode",
            telegram_html="Example result",
        )
        bot = ReceiptBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        self.addCleanup(sender.close)
        bot.receipt = None
        self.assertTrue(sender._deliver_one("opencode"))
        bot.receipt = 73
        self.assertTrue(sender._deliver_one("opencode"))
        self.assertEqual(self.state.get_telegram_outbox_for_job(first.job_id).status, "unknown")
        self.assertEqual(self.state.get_telegram_outbox_for_job(second.job_id).status, "delivered")
        self.assertEqual(self.state.get_provider_job(first.job_id).status, "failed")
        self.assertEqual(len(bot.sent), 2)
        counts = self.state.delivery.uncertain_counts()
        self.assertEqual(counts["nonblocking_notice_delivery"], 1)
        self.assertEqual(counts["outstanding_delivery_holds"], 0)


class LateProgressReceiptTests(unittest.TestCase):
    def test_shared_progress_helper_retries_only_local_commit_after_late_http(self) -> None:
        from tests.test_progress_delivery import DurableProgressDeliveryTests

        fixture = DurableProgressDeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job, token, journal = fixture.executing_job()
        journal.record_item(job, token, "example-progress", "Example progress", "commentary")
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        later = now + timedelta(minutes=4)
        progress = fixture.state.delivery.lease_progress("codex", "example-sender", now=now)
        assert progress and progress.lease_token
        bot = ReceiptBot()
        times = iter((now, later, later))
        original = fixture.state.delivery.mark_progress_delivered
        calls = 0

        def commit(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        with patch.object(fixture.state.delivery, "mark_progress_delivered", side_effect=commit):
            result = deliver_progress(
                fixture.state.delivery, bot, progress, clock=lambda: next(times)
            )
        self.assertTrue(result.receipt_committed)
        self.assertEqual(calls, 2)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(
            fixture.state.delivery.get_progress(progress.progress_id).status, "delivered"
        )
        self.assertEqual(fixture.state.get_provider_job(job).status, "executing")

    def test_expired_recovered_progress_records_exact_late_receipt_without_execution_change(
        self,
    ) -> None:
        from tests.test_progress_delivery import DurableProgressDeliveryTests

        fixture = DurableProgressDeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job, token, journal = fixture.executing_job()
        journal.record_item(job, token, "example-progress", "Example progress", "commentary")
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        progress = fixture.state.delivery.lease_progress("codex", "example-sender", now=now)
        assert progress and progress.lease_token
        fixture.state.delivery.begin_progress_send(
            progress.progress_id, progress.lease_token, now=now
        )
        later = now + timedelta(minutes=4)
        with closing(HubState.open_existing(fixture.config.state_path)) as second:
            second.delivery.recover_stale_progress(("codex",), now=later)
            second.delivery.mark_progress_delivered(
                progress.progress_id, progress.lease_token, telegram_message_id=77, now=later
            )
            second.delivery.mark_progress_delivered(
                progress.progress_id, progress.lease_token, telegram_message_id=77, now=later
            )
            self.assertEqual(second.delivery.get_progress(progress.progress_id).status, "delivered")
            self.assertEqual(second.get_provider_job(job).status, "executing")


if __name__ == "__main__":
    unittest.main()
