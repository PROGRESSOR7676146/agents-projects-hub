"""Saved-result delivery blockers use the independent Hub notice path."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock, patch

from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.state import HubState
from hermes_codex_router.task_notice_sender import deliver_task_notice
from tests import test_outbox_sender as sender_fixtures
from tests import test_queue_visibility as fixtures


class DeliveryWaitNoticeTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.QueueVisibilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.state = self.fixture.state
        self.state.reconcile_legacy_execution_scopes({"example-project": self.fixture.path.parent})
        self.job, self.outbox = self.fixture.saved_result("unknown")
        self.now = datetime.now(timezone.utc)

    def prepare(self):
        return self.state.delivery.prepare_delivery_wait_notices(
            self.state.task_notices, sender_agent_ids=("codex",), now=self.now
        )

    def reconcile(self):
        preview = self.state.preview_delivery_control("final_outbox", self.outbox.outbox_id)
        self.state.reconcile_delivery_control(
            "final_outbox",
            self.outbox.outbox_id,
            expected_snapshot=preview.snapshot,
            accept_unconfirmed_delivery=True,
        )

    def test_notice_reaches_same_topic_while_result_and_queued_tail_remain_blocked(self):
        tail, _ = self.fixture.enqueue(11, enabled=False)
        before = self.state.get_telegram_outbox_for_job(self.job.job_id)
        self.assertEqual(self.prepare(), 1)
        telegram = Mock()
        telegram.send_html.return_value = 901
        sent = deliver_task_notice(
            self.state.task_notices, telegram, "example-sender", now=self.now
        )
        self.assertTrue(sent.delivered)
        args, kwargs = telegram.send_html.call_args
        self.assertEqual(args[:2], (self.fixture.topic.chat_id, self.fixture.topic.thread_id))
        self.assertEqual(kwargs["reply_to_message_id"], 10)
        for fragment in (
            "saved",
            "unconfirmed",
            "investigate",
            "continue without confirmed delivery",
        ):
            self.assertIn(fragment, args[2])
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id), before)
        self.assertEqual(self.state.get_provider_job(tail.job_id).status, "queued")
        self.assertIsNone(self.state.lease_provider_job("codex", "example-worker"))

    def test_duplicate_and_restart_keep_one_notice_and_unknown_send_is_never_retried(self):
        self.assertEqual(self.prepare(), 1)
        telegram = Mock()
        telegram.send_html.side_effect = OSError("fictional network ambiguity")
        sent = deliver_task_notice(
            self.state.task_notices, telegram, "example-sender", now=self.now
        )
        self.assertIsNotNone(sent.error)
        self.state.close()
        self.state = HubState.open_existing(self.fixture.path, codex_permission_profile=None)
        self.fixture.state = self.state
        self.assertEqual(self.prepare(), 0)
        self.assertFalse(
            deliver_task_notice(
                self.state.task_notices, telegram, "example-sender", now=self.now
            ).worked
        )
        telegram.send_html.assert_called_once()

    def test_consent_after_notice_lease_suppresses_unattempted_stale_notice(self):
        self.prepare()
        lease = self.state.task_notices.lease_notice("example-sender", now=self.now)
        assert lease is not None
        self.reconcile()
        telegram = Mock()
        with patch.object(self.state.task_notices, "lease_notice", return_value=lease):
            deliver_task_notice(self.state.task_notices, telegram, "example-sender", now=self.now)
        telegram.send_html.assert_not_called()
        self.assertEqual(self.state.task_notices.get_notice(lease.notice_id).status, "superseded")

    def test_preparation_failure_preserves_unknown_result_and_is_retryable_as_notice_only(self):
        before = self.state.get_telegram_outbox_for_job(self.job.job_id)
        with patch.object(
            self.state.task_notices, "prepare_notice_in_transaction", side_effect=RuntimeError
        ):
            with self.assertRaises(RuntimeError):
                self.prepare()
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id), before)
        self.assertEqual(self.prepare(), 1)

    def test_other_senders_and_reconciled_delivery_prepare_no_blocker_notice(self):
        self.assertEqual(
            self.state.delivery.prepare_delivery_wait_notices(
                self.state.task_notices, sender_agent_ids=("opencode",), now=self.now
            ),
            0,
        )
        self.reconcile()
        self.assertEqual(self.prepare(), 0)

    def test_queue_only_release_also_suppresses_notice_without_confirming_delivery(self):
        preview = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=preview.snapshot,
            continue_without_confirmed_delivery=True,
        )
        self.assertEqual(self.prepare(), 0)
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id).status, "unknown")

    def test_legacy_invalid_destination_does_not_starve_another_topics_notice(self):
        # Fault injection only: normal current-schema writes reject this old shape.
        self.state._connection.execute("PRAGMA ignore_check_constraints=ON")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET thread_id=0 WHERE topic_id=?", (self.fixture.topic.topic_id,)
            )
            self.state._connection.execute(
                "UPDATE telegram_outbox SET thread_id=0 WHERE outbox_id=?",
                (self.outbox.outbox_id,),
            )
        self.state._connection.execute("PRAGMA ignore_check_constraints=OFF")
        topic, session = self.fixture.topic_session("example-project-other", 8)
        healthy, _ = self.fixture.enqueue(20, enabled=False, topic=topic, session=session)
        executing = self.fixture.start(healthy)
        self.state.commit_provider_result(
            healthy.job_id,
            executing.lease_token,
            visible_response="Fictional other result",
            sender_agent_id="codex",
            telegram_html="Fictional other result",
        )
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox SET status='unknown' WHERE job_id=?", (healthy.job_id,)
            )
        self.assertEqual(self.prepare(), 1)
        telegram = Mock()
        telegram.send_html.return_value = 902
        self.assertTrue(
            deliver_task_notice(
                self.state.task_notices, telegram, "example-sender", now=self.now
            ).delivered
        )
        self.assertEqual(telegram.send_html.call_args.args[:2], (topic.chat_id, topic.thread_id))


class DeliveryWaitSenderTests(TestCase):
    def setUp(self):
        self.fixture = sender_fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job_id = self.fixture.ready_outbox("opencode", 81)
        self.hub = sender_fixtures.Bot()
        self.other = sender_fixtures.Bot()
        config = replace(
            self.fixture.config,
            hub_bot=HubTelegramBot("example_hub_bot", self.fixture.base / "unused-token"),
        )
        self.sender = TelegramOutboxSender(
            config,
            telegram_bots={
                "hub": self.hub,
                "opencode": sender_fixtures.Bot(),
                "antigravity": self.other,
            },
        )
        self.addCleanup(self.sender.close)
        with self.sender.state._connection:
            self.sender.state._connection.execute(
                "UPDATE telegram_outbox SET status='unknown' WHERE job_id=?", (self.job_id,)
            )

    def test_ordinary_sender_cycle_delivers_notice_once_under_hub_identity(self):
        self.assertTrue(self.sender.run_cycle())
        self.assertEqual(len(self.hub.sent), 1)
        self.assertIn("unconfirmed", self.hub.sent[0][2])
        self.assertEqual(
            self.sender.state.get_telegram_outbox_for_job(self.job_id).status, "unknown"
        )
        self.sender.run_cycle()
        self.assertEqual(len(self.hub.sent), 1)

    def test_optional_notice_fault_does_not_block_another_topics_saved_result(self):
        self.fixture.ready_outbox("antigravity", 82)
        with patch.object(
            self.sender.state.delivery, "prepare_delivery_wait_notices", side_effect=OSError
        ):
            self.assertTrue(self.sender.run_cycle())
        self.assertEqual(len(self.other.sent), 1)
