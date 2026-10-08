"""Owner delivery disposition against fictional SQLite state; no live traffic."""

from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

from hermes_codex_router.state import HubState, StateError
from tests import test_outbox_sender as fixtures
from tests.test_delivery_certainty import ReceiptBot


class DeliveryHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job_id = self.fixture.ready_outbox("opencode", 81, telegram_html="Example " * 1800)
        self.bot = ReceiptBot()
        self.sender = self.fixture.sender(opencode=self.bot, antigravity=fixtures.Bot())
        self.addCleanup(self.sender.close)
        self.state = self.sender.state
        self.sender._deliver_one("opencode")
        self.bot.receipt = None
        self.sender._deliver_one("opencode")
        self.outbox = self.state.get_telegram_outbox_for_job(self.job_id)
        self.assertEqual(self.outbox.status, "unknown")

    def enqueue_tail(self, message_id: int = 82):
        job = self.state.get_provider_job(self.job_id)
        session = self.state.get_session(job.session_id)
        tail, _ = self.state.enqueue_provider_job(
            idempotency_key=f"telegram:-1001234567890:{message_id}",
            chat_id=job.chat_id,
            message_id=message_id,
            topic_id=job.topic_id,
            agent_id=job.agent_id,
            session_id=job.session_id,
            session_generation=job.session_generation,
            provider_session_id=job.provider_session_id,
            model=session.model,
            effort=session.effort,
            payload_text="Example successor",
            context_watermark=None,
            handoff_id=None,
        )
        return tail

    def release(self, state: HubState | None = None, token: str | None = None):
        owner = state or self.state
        token = token or owner.preview_delivery_hold(self.outbox.outbox_id).snapshot
        return owner.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=token,
            continue_without_confirmed_delivery=True,
        )

    def test_release_is_only_an_immutable_record_not_delivery_or_completion(self) -> None:
        before = self.state._connection.serialize()
        preview = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.assertEqual(self.state._connection.serialize(), before)
        self.assertEqual(preview.hold_status, "outstanding")
        self.assertEqual(preview.receipted_parts, 1)
        self.assertGreater(preview.part_count, 1)
        tables = (
            "provider_jobs",
            "provider_job_results",
            "telegram_outbox",
            "telegram_outbox_parts",
            "provider_execution_checkpoints",
            "provider_job_holds",
            "provider_stop_requests",
            "agent_sessions",
        )
        rows = {t: self.state._connection.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
        disposition = self.release(token=preview.snapshot)
        self.assertEqual(disposition.authority, "local_owner_cli")
        self.assertEqual(self.release(token=preview.snapshot), disposition)
        self.assertEqual(
            self.state.preview_delivery_hold(self.outbox.outbox_id).hold_status, "released_by_owner"
        )
        for table in tables:
            self.assertEqual(
                self.state._connection.execute(f"SELECT * FROM {table}").fetchall(), rows[table]
            )
        self.assertFalse(self.sender._deliver_one("opencode"))
        self.assertEqual(len(self.bot.sent), 2)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.state._connection:
                self.state._connection.execute("DELETE FROM telegram_delivery_hold_dispositions")

    def test_explicit_assertion_and_exact_snapshot_required(self) -> None:
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        for consent in (False, None, 1, "yes"):
            with self.subTest(consent=consent), self.assertRaises(StateError):
                self.state.release_delivery_hold(
                    self.outbox.outbox_id,
                    expected_snapshot=token,
                    continue_without_confirmed_delivery=consent,  # type: ignore[arg-type]
                )
        with self.assertRaises(StateError):
            self.release(token="0" * 64)
        self.assertEqual(
            self.state.preview_delivery_hold(self.outbox.outbox_id).hold_status, "outstanding"
        )

    def test_snapshot_covers_all_parts_beyond_64_and_receipt_provenance(self) -> None:
        db = self.state._connection
        with db:
            for index in range(10, 80):
                db.execute(
                    "INSERT INTO telegram_outbox_parts(outbox_id,part_index,telegram_html) VALUES(?,?,?)",
                    (self.outbox.outbox_id, index, "Example extra part"),
                )
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        with db:
            db.execute(
                "UPDATE telegram_outbox_parts SET receipt_validation_version=0 WHERE outbox_id=? AND part_index=1",
                (self.outbox.outbox_id,),
            )
        with self.assertRaises(StateError):
            self.release(token=token)
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        with db:
            db.execute(
                "UPDATE telegram_outbox_parts SET telegram_html='Changed' WHERE outbox_id=? AND part_index=79",
                (self.outbox.outbox_id,),
            )
        with self.assertRaises(StateError):
            self.release(token=token)

    def test_new_provider_job_and_later_outbox_pass_both_fifo_barriers(self) -> None:
        tail = self.enqueue_tail()
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        self.release()
        lease = self.state.lease_provider_job("opencode", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.assertEqual(lease.job_id, tail.job_id)
        self.state.mark_provider_job_executing(lease.job_id, lease.lease_token)
        self.state.commit_provider_result(
            lease.job_id,
            lease.lease_token,
            visible_response="Example next result",
            telegram_html="Example next result",
            sender_agent_id="opencode",
        )
        self.bot.receipt = 75
        self.assertTrue(self.sender._deliver_one("opencode"))
        self.assertEqual(self.state.get_telegram_outbox_for_job(tail.job_id).status, "delivered")
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job_id).status, "unknown")
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "result_ready")
        self.assertEqual(len(self.bot.sent), 3)

    def test_another_unknown_successor_requires_its_own_owner_decision(self) -> None:
        tail = self.enqueue_tail()
        self.release()
        lease = self.state.lease_provider_job("opencode", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(lease.job_id, lease.lease_token)
        self.state.commit_provider_result(
            lease.job_id,
            lease.lease_token,
            visible_response="Example second result",
            sender_agent_id="opencode",
            telegram_html="Example second result",
        )
        self.assertTrue(self.sender._deliver_one("opencode"))
        self.assertEqual(self.state.get_telegram_outbox_for_job(tail.job_id).status, "unknown")
        third = self.enqueue_tail(83)
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        next_outbox = self.state.get_telegram_outbox_for_job(tail.job_id)
        preview = self.state.preview_delivery_hold(next_outbox.outbox_id)
        self.state.release_delivery_hold(
            next_outbox.outbox_id,
            expected_snapshot=preview.snapshot,
            continue_without_confirmed_delivery=True,
        )
        next_lease = self.state.lease_provider_job("opencode", "example-worker")
        assert next_lease is not None
        self.assertEqual(next_lease.job_id, third.job_id)
        self.assertEqual(len(self.bot.sent), 3)

    def test_session_and_writer_changes_remain_blocked_by_retained_result(self) -> None:
        self.release()
        job = self.state.get_provider_job(self.job_id)
        with self.assertRaises(StateError):
            self.state.new_active_session(job.topic_id, expected_session_id=job.session_id)
        with self.assertRaises(StateError):
            self.state.set_writer_mode(job.session_id, "local")
        self.assertEqual(self.state.get_session(job.session_id).writer_mode, "telegram")

    def test_two_connections_and_uncertain_apply_retry_have_one_disposition(self) -> None:
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        with closing(HubState.open_existing(self.fixture.config.state_path)) as other:
            original = self.release(token=token)
            self.assertEqual(self.release(other, token), original)
            with self.assertRaises(StateError):
                self.release(other, "0" * 64)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_dispositions"
            ).fetchone()[0],
            1,
        )

    def test_read_only_preview_never_migrates_or_changes_database(self) -> None:
        path = self.fixture.config.state_path
        before = self.state._connection.serialize()
        with closing(HubState.open_read_only(path)) as reader:
            self.assertEqual(
                reader.preview_delivery_hold(self.outbox.outbox_id).snapshot,
                self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot,
            )
        self.assertEqual(self.state._connection.serialize(), before)

    def test_concurrent_connections_record_one_decision(self) -> None:
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        barrier = threading.Barrier(2)
        records, errors = [], []

        def apply() -> None:
            try:
                with closing(HubState.open_existing(self.fixture.config.state_path)) as owner:
                    barrier.wait(timeout=5)
                    records.append(self.release(owner, token))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=apply) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], records[1])

    def test_insert_fault_rolls_back_and_exact_retry_succeeds(self) -> None:
        with self.state._connection:
            self.state._connection.execute(
                "CREATE TRIGGER example_apply_fault AFTER INSERT ON telegram_delivery_hold_dispositions BEGIN SELECT RAISE(ABORT,'example fault'); END"
            )
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        with self.assertRaises(sqlite3.IntegrityError):
            self.release(token=token)
        self.assertEqual(
            self.state.preview_delivery_hold(self.outbox.outbox_id).hold_status, "outstanding"
        )
        with self.state._connection:
            self.state._connection.execute("DROP TRIGGER example_apply_fault")
        self.release(token=token)

    def test_native_uncertainty_and_known_local_writer_remain_independent(self) -> None:
        tail = self.enqueue_tail()
        self.release()
        db = self.state._connection
        with db:
            db.execute(
                "UPDATE provider_jobs SET status='indeterminate' WHERE job_id=?", (self.job_id,)
            )
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        with db:
            db.execute(
                "UPDATE provider_jobs SET status='result_ready' WHERE job_id=?", (self.job_id,)
            )
            db.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (tail.session_id,),
            )
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        with db:
            db.execute(
                "UPDATE agent_sessions SET writer_mode='telegram' WHERE session_id=?",
                (tail.session_id,),
            )
        self.assertIsNotNone(self.state.lease_provider_job("opencode", "example-worker"))

    def test_pending_owner_hold_and_emergency_stop_are_not_released(self) -> None:
        tail = self.enqueue_tail()
        with self.state._connection:
            self.state._connection.execute(
                "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at) VALUES(?,?,?)",
                (tail.job_id, self.job_id, datetime.now(timezone.utc).isoformat()),
            )
        self.release()
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        self.assertEqual(self.state.held_provider_job_count(tail.topic_id), 1)
        self.state.request_emergency_stop(
            topic_id=tail.topic_id, chat_id=tail.chat_id, message_id=83, target_agent_id="opencode"
        )
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        self.assertEqual(self.state.held_provider_job_count(tail.topic_id), 1)

    def test_changed_topic_binding_refuses_apply_and_invalidates_fifo_exception(self) -> None:
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        topic = self.state.get_provider_job(self.job_id).topic_id
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/changed' WHERE topic_id=?",
                (topic,),
            )
        with self.assertRaises(StateError):
            self.release(token=token)
        self.release()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/another' WHERE topic_id=?",
                (topic,),
            )
        self.assertEqual(self.state.reliability_snapshot()["outstanding_delivery_holds"], 1)
        preview = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.assertEqual(preview.hold_status, "disposition_binding_changed")
        self.assertIsNotNone(preview.disposition_snapshot)
        self.assertNotEqual(preview.snapshot, preview.disposition_snapshot)

    def test_unknown_with_missing_result_or_live_lease_refuses(self) -> None:
        token = self.state.preview_delivery_hold(self.outbox.outbox_id).snapshot
        with self.state._connection:
            self.state._connection.execute(
                "DELETE FROM provider_job_results WHERE job_id=?", (self.job_id,)
            )
        with self.assertRaises(StateError):
            self.release(token=token)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox SET status='sending',lease_owner='example',lease_token='example-token',lease_expires_at='2099-01-01T00:00:00+00:00' WHERE outbox_id=?",
                (self.outbox.outbox_id,),
            )
        with self.assertRaises(StateError):
            self.state.preview_delivery_hold(self.outbox.outbox_id)

    def test_released_unknown_remains_in_diagnostics_but_no_longer_alerts_as_blocked(self) -> None:
        from hermes_codex_router.reliability_alerts import evaluate_reliability_alerts

        before = self.state.reliability_snapshot()
        self.assertTrue(
            any(a.code == "unknown_delivery" for a in evaluate_reliability_alerts(before))
        )
        self.release()
        after = self.state.reliability_snapshot()
        self.assertFalse(
            any(a.code == "unknown_delivery" for a in evaluate_reliability_alerts(after))
        )
        outcome = self.state.provider_job_outcome(self.job_id).as_dict()
        self.assertEqual(outcome["result_delivery"]["status"], "unknown")
        self.assertEqual(outcome["result_delivery"]["delivery_hold"], "released_by_owner")
        self.assertFalse(outcome["productive_replay_authorized"])

    def test_fifo_diagnostics_skip_released_head_but_strict_busy_guards_remain(self) -> None:
        tail = self.enqueue_tail()
        now = datetime.now(timezone.utc) + timedelta(seconds=1000)
        with self.state._immediate_transaction():
            self.assertEqual(
                self.state.queue_visibility.wait_snapshot(tail.job_id, now=now).reason, "topic_fifo"
            )
        self.release()
        with self.state._immediate_transaction():
            self.assertNotEqual(
                self.state.queue_visibility.wait_snapshot(tail.job_id, now=now).reason, "topic_fifo"
            )
        activity = self.state.provider_chat_activities(("opencode",))
        self.assertEqual([a.message_id for a in activity], [82])
        self.assertTrue(self.state.topic_has_pending_provider_job(tail.topic_id))
        metrics = self.state.reliability_snapshot(now=now)
        self.assertEqual(metrics["unknown_delivery"], 1)
        self.assertEqual(metrics["outstanding_delivery_holds"], 0)
        self.assertEqual(metrics["released_delivery_holds"], 1)
