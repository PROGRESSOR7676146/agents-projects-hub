"""One fenced final part, shared by external and embedded senders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .artifacts import artifact_spool_root, remove_spooled_artifact, verify_spooled_artifact
from .delivery_retry import delivery_retry_delay, proven_delivery_rejection
from .state_delivery import DeliveryStateFacade, TelegramOutboxRecord
from .telegram import TelegramError


class FinalDeliveryTelegram(Protocol):
    def send_html(self, chat_id: int, thread_id: int, html: str) -> int: ...
    def send_document(
        self,
        chat_id: int,
        thread_id: int,
        document_path: Path,
        *,
        caption: str | None = None,
        file_name: str | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class FinalDeliveryResult:
    receipt_committed: bool = False
    error: Exception | None = None
    cleanup_error: Exception | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def delivery_error_code(error: Exception) -> str:
    return error.health_code if isinstance(error, TelegramError) else type(error).__name__[:128]


def deliver_final_part(
    delivery: DeliveryStateFacade,
    telegram: FinalDeliveryTelegram,
    outbox: TelegramOutboxRecord,
    *,
    state_path: Path,
    now: datetime | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FinalDeliveryResult:
    def current_time() -> datetime:
        return now if now is not None else clock()

    token = outbox.lease_token
    if token is None:
        raise ValueError("final delivery requires an existing lease")
    file_path: Path | None = None
    try:
        part = delivery.next_outbox_part(outbox.outbox_id, token, now=current_time())
        if part.part_type == "document":
            if (
                not part.file_path
                or not part.file_name
                or part.file_size is None
                or part.file_sha256 is None
            ):
                raise ValueError("artifact outbox metadata is incomplete")
            file_path = Path(part.file_path)
            verify_spooled_artifact(
                file_path,
                artifact_spool_root(state_path),
                expected_size=part.file_size,
                expected_sha256=part.file_sha256,
            )
        elif part.part_type != "text":
            raise ValueError("unsupported final part type")
    except Exception as error:
        # Local failure: no Telegram call. Retry only if this exact lease remains current.
        delivery.retry_outbox(
            outbox.outbox_id,
            token,
            error_code=delivery_error_code(error),
            delay_seconds=delivery_retry_delay(error, outbox.attempt_count),
            now=current_time(),
        )
        return FinalDeliveryResult(error=error)
    try:
        delivery.begin_outbox_send(outbox.outbox_id, token, part.part_index, now=current_time())
    except Exception as error:
        # A fence-commit failure may itself be ambiguous; never assume it did not commit.
        return FinalDeliveryResult(error=error)
    try:
        if file_path is not None:
            message_id = telegram.send_document(
                outbox.chat_id,
                outbox.thread_id,
                file_path,
                caption=part.telegram_html or None,
                file_name=part.file_name,
            )
        else:
            message_id = telegram.send_html(outbox.chat_id, outbox.thread_id, part.telegram_html)
    except Exception as error:
        if proven_delivery_rejection(error):
            delivery.retry_outbox(
                outbox.outbox_id,
                token,
                error_code=delivery_error_code(error),
                delay_seconds=delivery_retry_delay(error, outbox.attempt_count),
                now=current_time(),
            )
        else:
            delivery.mark_outbox_unknown(
                outbox.outbox_id,
                token,
                part.part_index,
                error_code=delivery_error_code(error),
                now=current_time(),
            )
        return FinalDeliveryResult(error=error)
    try:
        delivery.mark_outbox_delivered(
            outbox.outbox_id,
            token,
            telegram_message_id=message_id,
            part_index=part.part_index,
            now=current_time(),
        )
    except Exception as error:
        delivery.mark_outbox_unknown(
            outbox.outbox_id,
            token,
            part.part_index,
            error_code="receipt_commit_unknown",
            now=current_time(),
        )
        return FinalDeliveryResult(error=error)
    # Cleanup is outside transport/receipt catches: a fault cannot retry an accepted part.
    if file_path is not None:
        try:
            remove_spooled_artifact(file_path, artifact_spool_root(state_path))
        except Exception as error:
            return FinalDeliveryResult(receipt_committed=True, cleanup_error=error)
    return FinalDeliveryResult(receipt_committed=True)
