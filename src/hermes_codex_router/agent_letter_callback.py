"""Controller handling of the owner-only «Обработать» press on a letter (ADR 0112).

The press is checked against the exact confirmed Hub-bot display of the letter
in this numeric topic. It then becomes the owner's ordinary message in that
topic: a fixed request with the letter attached as a document, admitted through
normal ingress, so routing, FIFO, writers, stops, holds and approvals apply
unchanged. Admission is idempotent per pressed message; the press grants no
authority beyond the owner sending the same message and file.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .agent_letter_state import AgentLetterStore
from .agent_mail import done_keyboard, mime_type, parse_process_callback, process_text
from .diagnostic_log import survived
from .project_resolution import ProjectResolutionError, resolve_project_context
from .telegram import TelegramError, TopicCallback, _InternalSyntheticTopicUpdate


def handle_letter_process(service: Any, callback: TopicCallback) -> bool:
    """Admit one letter for the topic's current agent, once."""
    hub_bot = service.config.hub_bot
    if not service.config.agent_mail or hub_bot is None:
        _answer(service, callback.callback_id, "Почта агентов выключена")
        return True
    letter_id = parse_process_callback(callback.data)
    if (
        letter_id is None
        or callback.message_sender_id is None
        or callback.message_sender_id != hub_bot.telegram_user_id
    ):
        _answer(service, callback.callback_id, "Кнопка уже использована")
        return True
    letter = AgentLetterStore(service.state).displayed_letter(
        letter_id,
        chat_id=callback.chat_id,
        thread_id=callback.thread_id,
        message_id=callback.message_id,
    )
    if letter is None or not _project_current(service, callback.chat_id, letter.target_project_id):
        # Not this exact confirmed display, or the group now belongs elsewhere.
        _answer(service, callback.callback_id, "Это письмо нельзя передать агенту")
        return True
    record = f"letter:{callback.chat_id}:{callback.message_id}"
    if service.state.message_already_observed(callback.chat_id, callback.message_id) or (
        _recorded(service, record)
    ):
        _answer(service, callback.callback_id, "Кнопка уже использована")
        return True
    synthetic_message: dict[str, object] = {
        # The pressed Hub message id is unique in the chat and never a user
        # message, so durable admission is idempotent per letter.
        "message_id": callback.message_id,
        "from": {"id": callback.sender_id, "is_bot": False},
        "chat": {"id": callback.chat_id, "type": "supergroup", "is_forum": True},
        "caption": process_text(
            source_label=letter.source_label,
            agent_name=letter.source_agent_id,
            file_name=letter.file_name,
        ),
        "document": {
            "file_id": letter.file_id,
            "file_unique_id": letter.file_unique_id,
            "file_name": letter.file_name,
            "mime_type": mime_type(letter.file_name),
            "file_size": letter.byte_size,
        },
    }
    if callback.thread_id != 1:
        synthetic_message["message_thread_id"] = callback.thread_id
        synthetic_message["is_topic_message"] = True
    synthetic_update_id = -(
        int.from_bytes(hashlib.sha256(callback.callback_id.encode("utf-8")).digest()[:4], "big") + 1
    )
    # Admit first: until ingress accepts the message nothing marks the letter
    # as passed on, so a failure leaves the button usable for another press.
    if not service.handle_update(
        _InternalSyntheticTopicUpdate(update_id=synthetic_update_id, message=synthetic_message)
    ):
        return False
    service.state.claim_callback(
        record, observer_agent_id=getattr(service, "ingress_identity", service.agent.agent_id)
    )
    # Handled is not queued: ingress may have refused it, e.g. for a local writer.
    queued = _queued(service, callback.chat_id, callback.message_id)
    try:
        service.telegram.call(
            "editMessageReplyMarkup",
            chat_id=callback.chat_id,
            message_id=callback.message_id,
            reply_markup=json.dumps(done_keyboard(queued=queued), ensure_ascii=False),
        )
        service.telegram.answer_callback(
            callback.callback_id,
            "Письмо передано агенту" if queued else "Hub не принял письмо, причина — в теме",
        )
    except TelegramError as error:
        # The input is already admitted; a stale button is refused by the record.
        survived("agent_letter.markup", error)
    return True


def _answer(service: Any, callback_id: str, text: str) -> None:
    try:
        service.telegram.answer_callback(callback_id, text)
    except TelegramError as error:
        survived("agent_letter.markup", error)


def _project_current(service: Any, chat_id: int, project_id: str) -> bool:
    try:
        resolve_project_context(
            service.config, service.state, chat_id=chat_id, expected_project_id=project_id
        )
    except ProjectResolutionError:
        return False
    return True


def _queued(service: Any, chat_id: int, message_id: int) -> bool:
    return (
        service.state._connection.execute(
            "SELECT 1 FROM provider_job_inputs WHERE chat_id = ? AND message_id = ? LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
        is not None
    )


def _recorded(service: Any, record: str) -> bool:
    return (
        service.state._connection.execute(
            "SELECT 1 FROM observed_callbacks WHERE callback_id = ?", (record,)
        ).fetchone()
        is not None
    )
