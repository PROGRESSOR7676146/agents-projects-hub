"""Single Telegram-delivery boundary for durable root-blocker notices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from .delivery_retry import delivery_retry_delay

if TYPE_CHECKING:
    from .state import HubState


class BlockerTelegramSender(Protocol):
    def send_html(
        self,
        chat_id: int,
        thread_id: int,
        html: str,
        *,
        reply_markup: dict[str, object] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class BlockerSendResult:
    worked: bool
    error: Exception | None = None


def deliver_root_blocker_notice(
    state: HubState,
    telegram: BlockerTelegramSender,
    sender_id: str,
    *,
    now: datetime | None = None,
) -> BlockerSendResult:
    notice = state.lease_root_blocker_notice(sender_id, now=now)
    if notice is None:
        return BlockerSendResult(False)
    try:
        message_id = telegram.send_html(
            notice.chat_id,
            notice.thread_id,
            notice.telegram_html,
            reply_markup=notice.reply_markup,
            reply_to_message_id=notice.reply_to_message_id,
        )
        state.complete_root_blocker_notice(notice, message_id)
    except Exception as exc:
        state.retry_root_blocker_notice(
            notice, type(exc).__name__, delivery_retry_delay(exc, notice.attempt_count)
        )
        return BlockerSendResult(True, exc)
    return BlockerSendResult(True)
