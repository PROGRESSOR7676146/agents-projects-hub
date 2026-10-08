"""Owner delivery disposition against fictional SQLite state; no live traffic."""

from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

from hermes_codex_router.outbox_sender import TelegramOutboxSender
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
        self.state.reconcile_legacy_execution_scopes({"example-project": self.fixture.base})
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
        self.assertIn("result_ready_remains_without_time_limit", preview.control_consequences)
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
        disposition = self.release()
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/another' WHERE topic_id=?",
                (topic,),
            )
        # Simulate out-of-band damage, outside the trusted state/OS boundary.
        with self.state._connection:
            self.state._connection.execute(
                "DROP TRIGGER telegram_delivery_hold_topic_binding_guard"
            )
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/another' WHERE topic_id=?",
                (topic,),
            )
        self.assertEqual(self.state.reliability_snapshot()["outstanding_delivery_holds"], 1)
        preview = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.assertEqual(preview.hold_status, "disposition_binding_changed")
        self.assertIsNotNone(preview.disposition_snapshot)
        self.assertNotEqual(preview.snapshot, preview.disposition_snapshot)
        retried = self.release(token=disposition.snapshot)
        self.assertEqual(retried.snapshot, disposition.snapshot)
        self.assertEqual(retried.applied_at, disposition.applied_at)
        self.assertEqual(retried.hold_status, "disposition_binding_changed")
        self.assertEqual(
            self.state.provider_job_outcome(self.job_id).as_dict()["result_delivery"][
                "delivery_hold"
            ],
            "disposition_binding_changed",
        )

    def test_legacy_scope_refuses_and_recorded_binding_allows_only_noop_or_metadata_update(
        self,
    ) -> None:
        topic = self.state.get_provider_job(self.job_id).topic_id
        for scope in (None, "", "project:example-project"):
            with self.state._connection:
                self.state._connection.execute(
                    "UPDATE topics SET execution_scope=? WHERE topic_id=?", (scope, topic)
                )
            with self.assertRaisesRegex(StateError, "established canonical execution scope"):
                self.state.preview_delivery_hold(self.outbox.outbox_id)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/project' WHERE topic_id=?",
                (topic,),
            )
        record = self.release()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET title='Renamed',execution_scope=execution_scope WHERE topic_id=?",
                (topic,),
            )
        for column, value in (
            ("execution_scope", None),
            ("execution_scope", "project:example-project"),
            ("project_id", "another-project"),
            ("chat_id", 42),
            ("thread_id", 999),
        ):
            with (
                self.subTest(column=column, value=value),
                self.assertRaises(sqlite3.IntegrityError),
                self.state._connection,
            ):
                self.state._connection.execute(
                    f"UPDATE topics SET {column}=? WHERE topic_id=?", (value, topic)
                )
        self.assertEqual(self.release(token=record.snapshot), record)

    def test_failed_notice_release_unblocks_only_delivery_and_lane_binding_is_retained(
        self,
    ) -> None:
        self.release()
        failed = self.enqueue_tail()
        leased = self.state.lease_provider_job("opencode", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.terminate_provider_job_with_notice(
            failed.job_id,
            leased.lease_token,
            status="failed",
            expected_status="leased",
            error_class="preparation",
            error_code="example_failure",
            sender_agent_id="opencode",
            telegram_html="Example preparation failure",
        )
        self.bot.receipt = None
        self.sender._deliver_one("opencode")
        notice = self.state.get_telegram_outbox_for_job(failed.job_id)
        self.assertEqual(notice.status, "unknown")
        tail = self.enqueue_tail(83)
        leased = self.state.lease_provider_job("opencode", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(tail.job_id, leased.lease_token)
        self.state.commit_provider_result(
            tail.job_id,
            leased.lease_token,
            visible_response="Example tail",
            sender_agent_id="opencode",
            telegram_html="Example tail",
        )
        self.assertIsNone(self.state.lease_telegram_outbox("opencode", "example-sender"))
        preview = self.state.preview_delivery_hold(notice.outbox_id)
        self.assertIsNone(preview.result_id)
        self.assertEqual(
            preview.control_consequences, ("topic_binding_retained_for_disposition_lifetime",)
        )
        self.state.release_delivery_hold(
            notice.outbox_id,
            expected_snapshot=preview.snapshot,
            continue_without_confirmed_delivery=True,
        )
        self.bot.receipt = 333
        self.assertTrue(self.sender._deliver_one("opencode"))
        self.assertEqual(self.state.get_telegram_outbox_for_job(failed.job_id).status, "unknown")
        self.assertEqual(self.state.get_provider_job(failed.job_id).status, "failed")
        self.assertEqual(self.state.get_provider_job(tail.job_id).status, "completed")
        self.state.register_lane(
            lane_id="example-lane",
            project_id="example-project",
            worktree_path=self.fixture.base,
            branch_name="example-branch",
        )
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE worktree_lanes SET topic_id=? WHERE lane_id='example-lane'",
                (failed.topic_id,),
            )
        before = self.state.get_lane("example-lane")
        with self.assertRaisesRegex(StateError, "retains the topic binding"):
            self.state.archive_lane("example-lane")
        self.assertEqual(self.state.get_lane("example-lane"), before)
        topic = self.state.get_topic(failed.topic_id)
        renamed = self.state.observe_topic(
            project_id=topic.project_id,
            chat_id=topic.chat_id,
            thread_id=topic.thread_id,
            title="Renamed",
            execution_root=self.fixture.base,
        )
        self.assertEqual(renamed.execution_scope, topic.execution_scope)

    def test_unknown_hub_stop_notice_is_independent_and_has_no_final_outbox(self) -> None:
        config, stopped_job, request_id = self.fixture.stop_notice_fixture()
        hub = ReceiptBot()
        hub.receipt = None
        provider = fixtures.Bot()
        sender = TelegramOutboxSender(
            config, telegram_bots={"hub": hub, "opencode": provider, "antigravity": fixtures.Bot()}
        )
        self.addCleanup(sender.close)
        self.assertTrue(sender._deliver_task_notice_one())
        self.assertEqual(
            sender.state.task_notices.notices_for_stop(request_id)[0].status, "unknown"
        )
        with self.assertRaises(StateError):
            sender.state.get_telegram_outbox_for_job(stopped_job)
        tail = self.fixture.ready_outbox("opencode", 100)
        self.assertEqual(
            sender.state.get_provider_job(tail).topic_id,
            sender.state.get_provider_job(stopped_job).topic_id,
        )
        self.assertTrue(sender._deliver_one("opencode"))
        self.assertEqual(sender.state.get_provider_job(tail).status, "completed")
        self.assertFalse(sender._deliver_task_notice_one())
        self.assertEqual(len(hub.sent), 1)

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
        mixed = dict(
            after, unknown_delivery=3, outstanding_delivery_holds=1, released_delivery_holds=2
        )
        alert = next(a for a in evaluate_reliability_alerts(mixed) if a.code == "unknown_delivery")
        self.assertIn("1 outstanding unknown Telegram final delivery hold", alert.message)
        self.assertEqual(mixed["unknown_delivery"], 3)

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
