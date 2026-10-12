"""Advisory delivery uncertainty cannot mutate provider execution or receipts."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router.telegram import TelegramError
from tests import test_progress_delivery as fixtures


class ProgressCertaintyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.DurableProgressDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job_id, self.token, self.journal = self.fixture.executing_job()
        self.journal.record_item(
            self.job_id, self.token, "example-item", "Example progress", "commentary"
        )

    def test_invalid_receipt_is_unknown_and_cannot_be_superseded_or_resent(self) -> None:
        bot = fixtures.RecordingBot()
        sender = self.fixture.sender(bot)
        try:
            with patch.object(bot, "send_html", return_value=True) as transport:
                self.assertTrue(sender._deliver_progress_one("codex"))
                self.assertFalse(sender._deliver_progress_one("codex"))
                transport.assert_called_once()
            queue = sender.progress
            self.assertEqual(queue.for_job(self.job_id)[0].status, "unknown")
            self.assertEqual(sender.state.get_provider_job(self.job_id).status, "executing")
            self.fixture.state.fail_provider_job(
                self.job_id,
                self.token,
                error_class="provider_failure",
                error_code="example-terminal",
            )
            queue.supersede_terminal(("codex",))
            self.assertEqual(queue.for_job(self.job_id)[0].status, "unknown")
            self.assertEqual(
                sender.state.delivery.uncertain_counts()["unknown_progress_delivery"], 1
            )
        finally:
            sender.close()

    def test_restart_parks_attempted_and_requeues_unattempted(self) -> None:
        delivery = self.fixture.state.delivery
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        first = delivery.lease_progress("codex", "example-sender", now=now)
        assert first is not None and first.lease_token is not None
        delivery.recover_stale_progress(("codex",), now=now + timedelta(seconds=121))
        self.assertEqual(delivery.get_progress(first.progress_id).status, "pending")
        second = delivery.lease_progress(
            "codex", "example-sender", now=now + timedelta(seconds=122)
        )
        assert second is not None and second.lease_token is not None
        delivery.begin_progress_send(
            second.progress_id, second.lease_token, now=now + timedelta(seconds=122)
        )
        delivery.recover_stale_progress(("codex",), now=now + timedelta(seconds=243))
        self.assertEqual(delivery.get_progress(first.progress_id).status, "unknown")
        self.assertFalse(
            delivery.mark_progress_unknown(
                first.progress_id,
                first.lease_token,
                error_code="old-token",
                now=now + timedelta(seconds=244),
            )
        )

    def test_receipt_commit_failure_does_not_retry_even_with_api_rejection_type(self) -> None:
        bot = fixtures.RecordingBot()
        sender = self.fixture.sender(bot)
        try:
            with patch.object(
                sender.state.delivery,
                "mark_progress_delivered",
                side_effect=TelegramError("commit", failure_class="api_rejection", status_code=400),
            ):
                sender._deliver_progress_one("codex")
            self.assertEqual(sender.progress.for_job(self.job_id)[0].status, "unknown")
            self.assertFalse(sender._deliver_progress_one("codex"))
            self.assertEqual(len(bot.sent), 1)
        finally:
            sender.close()
