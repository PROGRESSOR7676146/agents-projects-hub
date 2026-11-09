"""Controller handling of one pressed reply-choice button.

The Controller authorizes the sender and resolves the topic before this runs.
Here the press is checked against the pressed message and the topic's current
Hub session, forwarded as the sender's ordinary text reply through normal
ingress, and only then recorded and acknowledged, so a failure cannot lose it. It adds no authority: the
result is the same as typing the label.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .diagnostic_log import survived
from .reply_choices import parse_choice_callback, session_binding
from .telegram import TelegramError, TopicCallback, _InternalSyntheticTopicUpdate


def handle_reply_choice(
    service: Any, callback: TopicCallback, project_id: str, topic_id: int
) -> bool:
    """Turn one pressed choice into the sender's ordinary reply, once."""
    parsed = parse_choice_callback(callback.data)
    labels = callback.choice_labels
    # The data must be exactly one button of the pressed message: a client can
    # send any callback data, so the binding is checked against the keyboard.
    if (
        parsed is None
        or parsed[0] >= len(labels)
        or callback.choice_data[parsed[0]] != callback.data
    ):
        _answer(service, callback.callback_id, "Выбор уже сделан")
        return True
    index, binding = parsed
    active = service.state.active_session(topic_id)
    if active is None or session_binding(active.session_id) != binding:
        _answer(service, callback.callback_id, "Эти варианты относятся к прошлой сессии")
        return True
    label = labels[index]
    record = f"choice:{callback.chat_id}:{callback.message_id}"
    if _choice_recorded(service, record):
        _answer(service, callback.callback_id, "Выбор уже сделан")
        return True
    synthetic_update_id = -(
        int.from_bytes(hashlib.sha256(callback.callback_id.encode("utf-8")).digest()[:4], "big") + 1
    )
    synthetic_message: dict[str, object] = {
        # The pressed Hub message id is unique in the chat and never a user message.
        # Durable admission is idempotent per (chat, message id), so a press that
        # is retried after a failure below cannot start a second turn.
        "message_id": callback.message_id,
        "from": {"id": callback.sender_id, "is_bot": False},
        "chat": {
            "id": callback.chat_id,
            "type": "supergroup",
            "title": project_id,
            "is_forum": True,
        },
        "text": label,
    }
    if callback.thread_id != 1:
        synthetic_message["message_thread_id"] = callback.thread_id
        synthetic_message["is_topic_message"] = True
    # Forward first: until ingress accepts the reply nothing marks the choice as
    # made, so a failure here leaves the buttons usable for another press.
    accepted = service.handle_update(
        _InternalSyntheticTopicUpdate(update_id=synthetic_update_id, message=synthetic_message)
    )
    if not accepted:
        return False
    # Admission is idempotent per pressed message: after a crash between
    # admission and the claim below, a later press of another button is
    # deduplicated, so the mark must show the label that was actually admitted.
    label = _admitted_label(service, callback, labels) or label
    service.state.claim_callback(
        record, observer_agent_id=getattr(service, "ingress_identity", service.agent.agent_id)
    )
    try:
        service.telegram.call(
            "editMessageReplyMarkup",
            chat_id=callback.chat_id,
            message_id=callback.message_id,
            reply_markup=json.dumps(
                {"inline_keyboard": [[{"text": f"✓ {label}", "callback_data": "ch:done"}]]},
                ensure_ascii=False,
            ),
        )
        service.telegram.answer_callback(callback.callback_id, f"Отправлено: {label}")
    except TelegramError as error:
        # The reply is already accepted; stale buttons are refused by the record.
        survived("reply_choice.markup", error)
    return True


def _answer(service: Any, callback_id: str, text: str) -> None:
    # An expired query ("query is too old") must not fail the press handling.
    try:
        service.telegram.answer_callback(callback_id, text)
    except TelegramError as error:
        survived("reply_choice.markup", error)


def _admitted_label(service: Any, callback: TopicCallback, labels: tuple[str, ...]) -> str | None:
    row = service.state._connection.execute(
        """SELECT input_text FROM provider_job_inputs
           WHERE chat_id = ? AND message_id = ? ORDER BY part_index LIMIT 1""",
        (callback.chat_id, callback.message_id),
    ).fetchone()
    text = None if row is None else row[0]
    return text if text in labels else None


def _choice_recorded(service: Any, record: str) -> bool:
    return (
        service.state._connection.execute(
            "SELECT 1 FROM observed_callbacks WHERE callback_id = ?", (record,)
        ).fetchone()
        is not None
    )
