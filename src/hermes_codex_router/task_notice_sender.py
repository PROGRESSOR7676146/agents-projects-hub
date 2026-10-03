"""One certainty-aware Telegram send for a durable task notice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .delivery_retry import delivery_retry_delay
from .task_lifecycle import TaskLifecycleState
from .telegram import TelegramError


class TaskNoticeTelegram(Protocol):
    def send_html(
        self, chat_id: int, thread_id: int, html: str, *, reply_to_message_id: int | None = None
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TaskNoticeDeliveryResult:
    worked: bool
    error: Exception | None = None
    delivered: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _proven_rejection(error: Exception) -> bool:
    if not isinstance(error, TelegramError):
        return False
    if error.failure_class == "api_http":
        return error.status_code == 429
    return (
        error.failure_class == "api_rejection"
        and error.status_code is not None
        and 400 <= error.status_code < 500
        and error.status_code != 408
    )


def deliver_task_notice(
    state: TaskLifecycleState,
    telegram: TaskNoticeTelegram,
    sender_id: str,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> TaskNoticeDeliveryResult:
    def current_time() -> datetime:
        return now if now is not None else clock()

    state.recover_expired_notices(now=current_time())
    notice = state.lease_notice(sender_id, now=current_time())
    if notice is None or notice.lease_token is None:
        return TaskNoticeDeliveryResult(False)
    token = notice.lease_token
    try:
        attempt = state.begin_send(notice.notice_id, token, now=current_time())
        if attempt.status == "superseded":
            return TaskNoticeDeliveryResult(True)
    except Exception as exc:
        # No transport call was made; lease recovery remains safely unattempted.
        return TaskNoticeDeliveryResult(True, exc)
    try:
        message_id = telegram.send_html(
            notice.chat_id,
            notice.thread_id,
            notice.telegram_html,
            reply_to_message_id=notice.reply_to_message_id,
        )
    except Exception as exc:
        timestamp = current_time()
        if _proven_rejection(exc):
            assert isinstance(exc, TelegramError)
            state.retry_rejected(
                notice.notice_id,
                token,
                error_code=exc.health_code,
                available_at=timestamp
                + timedelta(seconds=delivery_retry_delay(exc, attempt.attempt_count)),
                now=timestamp,
            )
        else:
            state.mark_send_unknown(
                notice.notice_id,
                token,
                error_code=exc.health_code
                if isinstance(exc, TelegramError)
                else type(exc).__name__,
                now=timestamp,
            )
        return TaskNoticeDeliveryResult(True, exc)
    try:
        state.complete_send(
            notice.notice_id, token, telegram_message_id=message_id, now=current_time()
        )
    except Exception as exc:
        # This includes invalid receipts and failure committing a positive receipt.
        # Neither is proof that Telegram rejected the message.
        state.mark_send_unknown(
            notice.notice_id, token, error_code="receipt_commit_unknown", now=current_time()
        )
        return TaskNoticeDeliveryResult(True, exc)
    return TaskNoticeDeliveryResult(True, delivered=True)
