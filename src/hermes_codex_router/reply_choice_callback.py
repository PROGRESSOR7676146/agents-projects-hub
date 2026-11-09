"""Controller handling of one pressed reply-choice button.

The Controller authorizes the sender and resolves the topic before this runs.
Here the press is checked against the pressed message and the topic's current
Hub session, claimed once per message, acknowledged, and then forwarded as the
sender's ordinary text reply through normal ingress. It adds no authority: the
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
    if parsed is None or parsed[0] >= len(labels):
        service.telegram.answer_callback(callback.callback_id, "Выбор уже сделан")
        return True
    index, binding = parsed
    active = service.state.active_session(topic_id)
    if active is None or session_binding(active.session_id) != binding:
        service.telegram.answer_callback(
            callback.callback_id, "Эти варианты относятся к прошлой сессии"
        )
        return True
    label = labels[index]
    # First press wins across buttons, duplicate deliveries and restarts.
    if not service.state.claim_callback(
        f"choice:{callback.chat_id}:{callback.message_id}",
        observer_agent_id=getattr(service, "ingress_identity", service.agent.agent_id),
    ):
        service.telegram.answer_callback(callback.callback_id, "Выбор уже сделан")
        return True
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
    except TelegramError as error:
        # The reply below is the outcome; stale buttons are refused by the claim.
        survived("reply_choice.markup", error)
    service.telegram.answer_callback(callback.callback_id, f"Отправлено: {label}")
    synthetic_update_id = -(
        int.from_bytes(hashlib.sha256(callback.callback_id.encode("utf-8")).digest()[:4], "big") + 1
    )
    synthetic_message: dict[str, object] = {
        # The pressed Hub message id is unique in the chat and never a user message.
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
    return service.handle_update(
        _InternalSyntheticTopicUpdate(update_id=synthetic_update_id, message=synthetic_message)
    )
