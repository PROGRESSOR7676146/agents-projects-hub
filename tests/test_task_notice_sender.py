from __future__ import annotations

from datetime import timedelta
from typing import Any

from hermes_codex_router.task_notice_sender import deliver_task_notice
from hermes_codex_router.telegram import TelegramError
from tests.test_task_lifecycle import TaskLifecycleFixture


class FakeTelegram:
    def __init__(self, *, receipt: Any = 23, error: Exception | None = None):
        self.receipt = receipt
        self.error = error
        self.calls = []

    def send_html(self, chat_id, thread_id, html, *, reply_to_message_id=None):
        self.calls.append((chat_id, thread_id, html, reply_to_message_id))
        if self.error is not None:
            raise self.error
        return self.receipt


class TaskNoticeSenderTests(TaskLifecycleFixture):
    def deliver(self, bot, **changes):
        return deliver_task_notice(self.state, bot, "sender-a", now=self.now, **changes)

    def test_delivers_one_bound_notice_without_provider_side_effects(self) -> None:
        notice, _ = self.prepare()
        bot = FakeTelegram()
        result = self.deliver(bot)
        self.assertTrue(result.worked)
        self.assertIsNone(result.error)
        self.assertEqual(bot.calls[0][0:2], (-1001234567890, 7))
        self.assertEqual(bot.calls[0][3], 10)
        delivered = self.state.get_notice(notice.notice_id)
        self.assertEqual((delivered.status, delivered.telegram_message_id), ("delivered", 23))
        self.assertFalse(self.deliver(bot).worked)
        self.assertEqual(len(bot.calls), 1)

    def test_network_timeout_is_unknown_without_blind_resend(self) -> None:
        notice, _ = self.prepare()
        bot = FakeTelegram(
            error=TelegramError(
                "timeout", operation="send_message", failure_class="network_timeout"
            )
        )
        result = self.deliver(bot)
        self.assertIsNotNone(result.error)
        self.assertEqual(self.state.get_notice(notice.notice_id).status, "unknown")
        self.assertFalse(self.deliver(bot).worked)
        self.assertEqual(len(bot.calls), 1)

    def test_only_structured_explicit_api_rejection_retries(self) -> None:
        notice, _ = self.prepare()
        bot = FakeTelegram(
            error=TelegramError(
                "rate limited",
                operation="send_message",
                failure_class="api_rejection",
                status_code=429,
                retry_after=60,
            )
        )
        self.deliver(bot)
        pending = self.state.get_notice(notice.notice_id)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.available_at, (self.now + timedelta(seconds=60)).isoformat())
        self.assertFalse(self.deliver(bot).worked)
        bot.error = None
        self.now += timedelta(seconds=60)
        self.assertTrue(self.deliver(bot).worked)
        self.assertEqual(self.state.get_notice(notice.notice_id).attempt_count, 2)

    def test_http_5xx_and_unclassified_errors_remain_unknown(self) -> None:
        for error in (
            TelegramError(
                "server error", operation="send_message", failure_class="api_http", status_code=500
            ),
            TelegramError("bad result", operation="send_message", failure_class="invalid_response"),
            RuntimeError("transport raised"),
        ):
            with self.subTest(error=error):
                notice, _ = self.prepare(event_key=type(error).__name__ + str(error))
                self.deliver(FakeTelegram(error=error))
                self.assertEqual(self.state.get_notice(notice.notice_id).status, "unknown")

    def test_false_zero_or_missing_receipt_is_unknown(self) -> None:
        for receipt in (False, True, 0, None):
            with self.subTest(receipt=receipt):
                notice, _ = self.prepare(event_key="receipt:" + str(receipt))
                result = self.deliver(FakeTelegram(receipt=receipt))
                self.assertIsNotNone(result.error)
                self.assertEqual(self.state.get_notice(notice.notice_id).status, "unknown")

    def test_failure_persisting_positive_receipt_is_unknown(self) -> None:
        notice, _ = self.prepare()
        original = self.state.complete_send

        def fail_commit(*args, **kwargs):
            raise RuntimeError("receipt transaction failed")

        self.state.complete_send = fail_commit
        bot = FakeTelegram()
        result = self.deliver(bot)
        self.state.complete_send = original
        self.assertIsNotNone(result.error)
        self.assertEqual(self.state.get_notice(notice.notice_id).status, "unknown")
        self.assertFalse(self.deliver(bot).worked)
        self.assertEqual(len(bot.calls), 1)

    def test_supplied_now_keeps_attempt_timestamps_deterministic(self) -> None:
        notice, _ = self.prepare()

        def forbidden_clock():
            raise AssertionError("real clock called despite supplied now")

        self.deliver(FakeTelegram(), clock=forbidden_clock)
        delivered = self.state.get_notice(notice.notice_id)
        self.assertEqual(delivered.updated_at, self.now.isoformat())

    def test_expired_begun_attempt_is_not_sent_on_restart(self) -> None:
        notice, _ = self.started()
        self.now += timedelta(seconds=100)
        bot = FakeTelegram()
        self.assertFalse(self.deliver(bot).worked)
        self.assertEqual(bot.calls, [])
        self.assertEqual(self.state.get_notice(notice.notice_id).status, "unknown")

    def test_exception_after_receipt_commit_preserves_affirmative_evidence(self) -> None:
        notice, _ = self.prepare()
        original = self.state.complete_send

        def committed_then_raised(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("post-commit fault")

        self.state.complete_send = committed_then_raised
        bot = FakeTelegram()
        self.assertIsNotNone(self.deliver(bot).error)
        self.state.complete_send = original
        delivered = self.state.get_notice(notice.notice_id)
        self.assertEqual((delivered.status, delivered.telegram_message_id), ("delivered", 23))
        self.assertFalse(self.deliver(bot).worked)
        self.assertEqual(len(bot.calls), 1)

    def test_repeated_explicit_rejection_eventually_fails_without_provider_changes(self) -> None:
        notice, _ = self.prepare()
        bot = FakeTelegram(
            error=TelegramError(
                "rejected", operation="send_message", failure_class="api_rejection", status_code=400
            )
        )
        for _ in range(5):
            self.assertTrue(self.deliver(bot).worked)
            self.now += timedelta(seconds=600)
        failed = self.state.get_notice(notice.notice_id)
        self.assertEqual((failed.status, failed.attempt_count), ("failed", 5))
        self.assertFalse(self.deliver(bot).worked)
        self.assertEqual(len(bot.calls), 5)
        self.assertEqual(
            self.db.execute("SELECT status FROM provider_jobs").fetchone()[0], "executing"
        )

    def test_native_http_429_is_rejected_and_retains_retry_after(self) -> None:
        notice, _ = self.prepare()
        bot = FakeTelegram(
            error=TelegramError(
                "HTTP rate limit",
                operation="send_message",
                failure_class="api_http",
                status_code=429,
                retry_after=60,
            )
        )
        self.deliver(bot)
        retained = self.state.get_notice(notice.notice_id)
        self.assertEqual(retained.status, "pending")
        self.assertEqual(retained.available_at, (self.now + timedelta(seconds=60)).isoformat())
        self.assertFalse(self.deliver(bot).worked)
        self.assertEqual(len(bot.calls), 1)

    def test_structured_500_408_or_missing_status_cannot_prove_rejection(self) -> None:
        for status in (500, 408, None):
            with self.subTest(status=status):
                notice, _ = self.prepare(event_key="api-rejection:" + str(status))
                self.deliver(
                    FakeTelegram(
                        error=TelegramError(
                            "API outcome",
                            operation="send_message",
                            failure_class="api_rejection",
                            status_code=status,
                        )
                    )
                )
                self.assertEqual(self.state.get_notice(notice.notice_id).status, "unknown")

    def test_http_400_without_a_structured_rejection_remains_unknown(self) -> None:
        notice, _ = self.prepare()
        self.deliver(
            FakeTelegram(
                error=TelegramError(
                    "HTTP error",
                    operation="send_message",
                    failure_class="api_http",
                    status_code=400,
                )
            )
        )
        self.assertEqual(self.state.get_notice(notice.notice_id).status, "unknown")
