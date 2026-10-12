"""Fenced advisory progress send; execution state remains independent."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Protocol

from .delivery_retry import delivery_retry_delay, proven_delivery_rejection
from .final_delivery import FinalDeliveryResult, delivery_error_code, utc_now
from .state_delivery import DeliveryStateFacade, ProgressDeliveryRecord


class ProgressTelegram(Protocol):
    def send_html(
        self, chat_id: int, thread_id: int, html: str, *, disable_notification: bool = False
    ) -> int: ...


def deliver_progress(
    delivery: DeliveryStateFacade,
    telegram: ProgressTelegram,
    progress: ProgressDeliveryRecord,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FinalDeliveryResult:
    def current_time() -> datetime:
        return now if now is not None else clock()

    token = progress.lease_token
    if token is None:
        raise ValueError("progress delivery requires an existing lease")
    try:
        delivery.begin_progress_send(progress.progress_id, token, now=current_time())
    except Exception as error:
        return FinalDeliveryResult(error=error)
    try:
        message_id = telegram.send_html(
            progress.chat_id, progress.thread_id, progress.telegram_html, disable_notification=True
        )
    except Exception as error:
        if proven_delivery_rejection(error):
            delivery.retry_progress(
                progress.progress_id,
                token,
                error_code=delivery_error_code(error),
                delay_seconds=delivery_retry_delay(error, progress.attempt_count),
                now=current_time(),
            )
        else:
            delivery.mark_progress_unknown(
                progress.progress_id,
                token,
                error_code=delivery_error_code(error),
                now=current_time(),
            )
        return FinalDeliveryResult(error=error)
    try:
        delivery.mark_progress_delivered(
            progress.progress_id, token, telegram_message_id=message_id, now=current_time()
        )
    except Exception as error:
        delivery.mark_progress_unknown(
            progress.progress_id, token, error_code="receipt_commit_unknown", now=current_time()
        )
        return FinalDeliveryResult(error=error)
    return FinalDeliveryResult(receipt_committed=True)
