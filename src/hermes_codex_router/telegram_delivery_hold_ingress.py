"""Hub-Bot ingress for one owner-approved unknown delivery hold.

This adapter owns only the established Hub command/callback path.  Snapshot
creation and immutable CAS remain in the state/domain helpers; this module has
no provider, sender, receipt or retry authority.
"""

from __future__ import annotations

import html
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Protocol

from . import artifact_capabilities
from .delivery_retry import proven_delivery_rejection
from .hub_config import AgentDefinition, HubConfig
from .routing import parse_command
from .state import HubState, StateError
from .telegram import TelegramBotApi, TelegramError, TopicCallback, TopicMessage
from .telegram_delivery_hold_state import (
    CALLBACK_ORIGIN,
    CALLBACK_PREFIX,
    COPY_CALLBACK_PREFIX,
    preview_text,
)

UNAVAILABLE = (
    "Remote delivery recovery is unavailable in this build. Use the local delivery-hold command."
)


class _HubDeliveryHoldService(Protocol):
    """The small established-service surface this adapter needs."""

    config: HubConfig
    state: HubState
    agent: AgentDefinition
    telegram: TelegramBotApi

    def _send_text(self, message: TopicMessage, text: str) -> None: ...


def _recovery_enabled(service: _HubDeliveryHoldService) -> bool:
    """Keep remote recovery inside the already-owned Hub external-queue path."""
    return (
        getattr(service, "ingress_identity", service.agent.agent_id) == "hub"
        and not getattr(service, "direct_messages_only", False)
        and service.config.hub_delivery_hold_recovery_enabled
    )


def _same_telegram_username(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.removeprefix("@").casefold() == right.removeprefix("@").casefold()


def _literal_delivery_command(text: str) -> bool:
    """Keep this exceptional remote authority to its one documented command form."""
    return text.strip().casefold() == "/delivery"


def _looks_like_delivery_recovery_command(text: str) -> bool:
    """A typo of this reserved control is never a productive provider prompt."""
    return text.strip().casefold().startswith("/delivery")


def _refusal(service: _HubDeliveryHoldService, message: TopicMessage, text: str) -> bool:
    if not service.state.claim_message(
        message.chat_id,
        message.message_id,
        observer_agent_id="hub",
    ):
        return False
    service._send_text(message, text)
    return True


def try_handle_telegram_delivery_hold_command(
    service: _HubDeliveryHoldService, message: TopicMessage
) -> bool | None:
    """Prepare, send, then bind one non-replayable Hub owner preview."""
    command = parse_command(message.text)
    if command is None or command.name != "delivery":
        if _looks_like_delivery_recovery_command(message.text):
            if not artifact_capabilities.REMOTE_DELIVERY_HOLD_RECOVERY:
                return _refusal(service, message, UNAVAILABLE)
            return _refusal(
                service,
                message,
                "Reply /delivery without text additions or attachments to the exact Hub delivery warning.",
            )
        return None
    if message.sender_id not in service.config.owner_user_ids:
        # A scoped acceptance actor can pass general ingress authorization,
        # but is never an owner for this recovery capability.
        return False
    if not artifact_capabilities.REMOTE_DELIVERY_HOLD_RECOVERY:
        return _refusal(service, message, UNAVAILABLE)
    if (
        message.is_forwarded
        or command.arguments
        or message.attachments
        or message.unavailable_materials
        or not _literal_delivery_command(message.text)
    ):
        return _refusal(
            service,
            message,
            "Reply /delivery without text additions or attachments to the exact Hub delivery warning.",
        )
    if (
        not _recovery_enabled(service)
        or message.chat_id == message.sender_id
        or service.config.hub_bot is None
        or service.config.hub_bot.telegram_user_id is None
    ):
        return _refusal(
            service,
            message,
            "This delivery recovery is available only through the configured Hub in a project topic.",
        )
    hub_bot = service.config.hub_bot
    assert hub_bot is not None and hub_bot.telegram_user_id is not None
    if (
        message.reply_to_message_id is None
        or message.reply_to_sender_id != hub_bot.telegram_user_id
        or not _same_telegram_username(message.reply_to_username, hub_bot.telegram_username)
    ):
        return _refusal(
            service,
            message,
            "Reply /delivery to the current Hub delivery warning in this topic.",
        )
    if not service.state.claim_message(
        message.chat_id,
        message.message_id,
        observer_agent_id="hub",
    ):
        return False
    try:
        preview = service.state.prepare_telegram_delivery_hold_preview(
            owner_user_id=message.sender_id,
            owner_user_ids=service.config.owner_user_ids,
            hub_bot_user_id=hub_bot.telegram_user_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            notice_message_id=message.reply_to_message_id,
        )
    except StateError:
        service._send_text(
            message,
            "That delivery warning is no longer eligible for recovery. Reply /delivery to the current Hub warning.",
        )
        return True
    try:
        preview_message_id = service.telegram.send_html(
            message.chat_id,
            message.thread_id,
            preview_text(preview),
            reply_to_message_id=message.reply_to_message_id,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Continue without confirmed delivery",
                            "callback_data": preview.callback_data,
                        }
                    ],
                    [
                        {
                            "text": "Send me a copy of the saved answer",
                            "callback_data": preview.copy_callback_data,
                        }
                    ],
                ]
            },
        )
    except TelegramError:
        # A message may or may not have reached Telegram; do not bind a
        # capability or retry the preview through another path.
        return True
    if (
        not isinstance(preview_message_id, int)
        or isinstance(preview_message_id, bool)
        or preview_message_id <= 0
    ):
        return True
    try:
        service.state.bind_telegram_delivery_hold_preview(
            preview,
            owner_user_ids=service.config.owner_user_ids,
            preview_message_id=preview_message_id,
        )
    except (StateError, sqlite3.Error):
        # A positive transport return without a durable bind is deliberately
        # unusable rather than an invitation to guess or resend.
        return True
    return True


def try_handle_telegram_delivery_hold_callback(
    service: _HubDeliveryHoldService, callback: TopicCallback
) -> bool | None:
    """Apply a bound owner callback without the generic callback receipt gate."""
    is_copy = callback.data.startswith(COPY_CALLBACK_PREFIX)
    if not is_copy and not callback.data.startswith(CALLBACK_PREFIX):
        return None
    if not artifact_capabilities.REMOTE_DELIVERY_HOLD_RECOVERY:
        service.telegram.answer_callback(callback.callback_id, UNAVAILABLE)
        return True
    hub_bot = service.config.hub_bot
    if (
        not _recovery_enabled(service)
        or hub_bot is None
        or hub_bot.telegram_user_id is None
        or callback.chat_id == callback.sender_id
        or callback.sender_id not in service.config.owner_user_ids
        or callback.message_sender_id != hub_bot.telegram_user_id
        or not _same_telegram_username(callback.message_sender_username, hub_bot.telegram_username)
    ):
        service.telegram.answer_callback(
            callback.callback_id, "This recovery preview is unavailable"
        )
        return True
    token = callback.data.removeprefix(COPY_CALLBACK_PREFIX if is_copy else CALLBACK_PREFIX)
    if is_copy:
        try:
            result, title, has_attachments = service.state.read_telegram_delivery_hold_private_copy(
                token,
                owner_user_id=callback.sender_id,
                owner_user_ids=service.config.owner_user_ids,
                chat_id=callback.chat_id,
                thread_id=callback.thread_id,
                callback_message_id=callback.message_id,
                hub_bot_user_id=hub_bot.telegram_user_id,
                callback_origin=CALLBACK_ORIGIN,
            )
        except (StateError, sqlite3.Error):
            service.telegram.answer_callback(
                callback.callback_id, "Предпросмотр недоступен. Ответьте /delivery ещё раз."
            )
            return True
        label = (
            f"Копия сохранённого ответа из темы «{html.escape(title[:128])}». "
            "Доставка оригинала не подтверждена; оригинал уже может быть в теме."
            + (" Вложения оригинала не включены." if has_attachments else "")
        )
        try:
            body = label + "\n\n" + html.escape(result)
            if len(body.encode("utf-16-le")) // 2 <= 3500:
                service.telegram.send_html(callback.sender_id, 1, body)
            else:
                with tempfile.TemporaryDirectory(prefix="hub-delivery-copy-") as directory:
                    path = Path(directory) / "saved-answer.md"
                    # The private temporary directory is mode 0700; the file is also 0600.
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                        file.write(result)
                    service.telegram.send_document(
                        callback.sender_id,
                        1,
                        path,
                        caption=label,
                        file_name="saved-answer.md",
                        mime_type="text/markdown",
                    )
        except TelegramError as error:
            if error.private_chat_unavailable:
                try:
                    service.telegram.send_html(
                        callback.chat_id,
                        callback.thread_id,
                        "Откройте личный чат с ботом, нажмите Start, затем нажмите кнопку ещё раз.",
                    )
                except TelegramError:
                    pass
                service.telegram.answer_callback(callback.callback_id, "Личный чат недоступен.")
            elif proven_delivery_rejection(error):
                service.telegram.answer_callback(callback.callback_id, "Telegram отклонил копию.")
            else:
                service.telegram.answer_callback(
                    callback.callback_id, "Копия могла не доставиться; нажмите ещё раз."
                )
            return True
        except OSError:
            service.telegram.answer_callback(
                callback.callback_id, "Не удалось подготовить копию; нажмите ещё раз."
            )
            return True
        service.telegram.answer_callback(callback.callback_id, "Копия отправлена в личный чат.")
        return True
    try:
        disposition = service.state.apply_telegram_delivery_hold_callback(
            token,
            owner_user_id=callback.sender_id,
            owner_user_ids=service.config.owner_user_ids,
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            callback_message_id=callback.message_id,
            hub_bot_user_id=hub_bot.telegram_user_id,
            callback_id=callback.callback_id,
            callback_origin=CALLBACK_ORIGIN,
        )
    except (StateError, sqlite3.Error):
        service.telegram.answer_callback(
            callback.callback_id,
            "This recovery preview is unavailable. Reply /delivery to the current Hub warning.",
        )
        return True
    service.telegram.answer_callback(
        callback.callback_id,
        "Later queued work may continue; Telegram delivery remains unconfirmed."
        if disposition.hold_status == "released_by_telegram_owner"
        else "The decision is recorded, but this topic binding no longer permits queue continuation.",
    )
    return True
