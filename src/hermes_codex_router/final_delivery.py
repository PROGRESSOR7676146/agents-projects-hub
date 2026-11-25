"""One fenced final part, shared by external and embedded senders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import artifact_capabilities
from .artifacts import artifact_spool_root, remove_spooled_artifact, verify_spooled_artifact
from .codex_failure import owner_resolution_action
from .delivery_retry import delivery_retry_delay, retryable_delivery_failure
from .receipt_commit import commit_receipt
from .reply_choices import CHOICE_PROMPT_HTML, keyboard, parse_choice_part
from .session_handoff import HANDOFF_PROMPT_HTML, parse_handoff_part
from .session_handoff import keyboard as handoff_keyboard
from .state_delivery import DeliveryStateFacade, TelegramOutboxRecord
from .telegram import TelegramError
from .uncertain_decision import PROMPT_HTML as UNCERTAIN_PROMPT_HTML
from .uncertain_decision import keyboard as uncertain_keyboard
from .uncertain_decision import parse_prompt_part


class FinalDeliveryTelegram(Protocol):
    def send_html(
        self,
        chat_id: int,
        thread_id: int,
        html: str,
        *,
        reply_markup: dict[str, object] | None = None,
    ) -> int: ...
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
    choice_telegram: FinalDeliveryTelegram | None = None,
) -> FinalDeliveryResult:
    # Buttons are answered through the bot that sent them. The Controller polls
    # the Hub bot, so choice buttons use it rather than the provider bot.
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
        delivery.begin_outbox_send(
            outbox.outbox_id,
            token,
            part.part_index,
            now=current_time(),
            expected_outbox=outbox,
            expected_part=part,
        )
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
        elif (choice := parse_choice_part(part.telegram_html)) is not None and choice[1]:
            message_id = (choice_telegram or telegram).send_html(
                outbox.chat_id,
                outbox.thread_id,
                CHOICE_PROMPT_HTML,
                reply_markup=keyboard(*choice),
            )
        elif (handoff := parse_handoff_part(part.telegram_html)) is not None and handoff[1]:
            # The Hub bot receives the press (ADR 0114), as for choices.
            message_id = (choice_telegram or telegram).send_html(
                outbox.chat_id,
                outbox.thread_id,
                HANDOFF_PROMPT_HTML,
                reply_markup=handoff_keyboard(*handoff),
            )
        elif (snapshot := parse_prompt_part(part.telegram_html)) is not None:
            # Only the Hub bot receives the press (ADR 0107); without it the
            # prompt degrades to the local owner command, never provider buttons.
            message_id = (
                choice_telegram.send_html(
                    outbox.chat_id,
                    outbox.thread_id,
                    UNCERTAIN_PROMPT_HTML,
                    reply_markup=uncertain_keyboard(snapshot),
                )
                if choice_telegram is not None
                else telegram.send_html(
                    outbox.chat_id, outbox.thread_id, owner_resolution_action(outbox.job_id)
                )
            )
        else:
            message_id = telegram.send_html(outbox.chat_id, outbox.thread_id, part.telegram_html)
    except Exception as error:
        if retryable_delivery_failure(error):
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
        delivered = commit_receipt(
            lambda: delivery.mark_outbox_delivered(
                outbox.outbox_id,
                token,
                telegram_message_id=message_id,
                part_index=part.part_index,
                now=current_time(),
            )
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
    # The schema-58 rollback artifact still commits exact retirement evidence,
    # but retains spool files.  A candidate that enables cleanup may remove
    # only documents whose complete outbox proof committed first.
    if artifact_capabilities.RECEIPT_BOUND_ARTIFACT_CLEANUP and delivered.status == "delivered":
        for delivered_part in delivery.get_outbox_parts(delivered.outbox_id):
            if (
                delivered_part.part_type != "document"
                or not delivered_part.file_path
                or not delivery.artifact_retired(delivered.outbox_id, delivered_part.part_index)
            ):
                continue
            try:
                remove_spooled_artifact(
                    Path(delivered_part.file_path), artifact_spool_root(state_path)
                )
            except Exception as error:
                return FinalDeliveryResult(receipt_committed=True, cleanup_error=error)
    return FinalDeliveryResult(receipt_committed=True)
