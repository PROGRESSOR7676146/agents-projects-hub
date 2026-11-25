"""Controller handling of «Новая сессия с handoff» (ADR 0114).

The Controller authorizes the owner and resolves the topic before this runs.
Authority comes from the exact delivered control: the pressed Hub-bot message
must be the recorded handoff part of the source job's answer in this numeric
topic. The request text and the chosen option are rebuilt from the saved
answer, never taken from Telegram. One admission transaction archives the
session that wrote the answer, opens its successor and admits the fixed owner
message as the successor's first input; any guard failure commits nothing.
"""

from __future__ import annotations

import json
from typing import Any

from .controller_admission import (
    CommittedAdmission,
    DuplicateAdmission,
    DurableAdmissionRequest,
    RejectedAdmission,
    controller_provider_admission,
)
from .diagnostic_log import survived
from .session_handoff import (
    HANDOFF_DONE_CALLBACK,
    done_keyboard,
    extract_handoff,
    fits,
    handoff_binding,
    parse_handoff_callback,
    parse_handoff_part,
    request_text,
)
from .state import StateError
from .telegram import TelegramError, TopicCallback, TopicMessage

STALE = "Кнопка устарела"
USED = "Кнопка уже использована"


def handle_session_handoff(
    service: Any, callback: TopicCallback, project_id: str, topic_id: int
) -> bool:
    """Start the successor session with the saved answer as its first input, once."""
    hub_bot = service.config.hub_bot
    if not getattr(service.config, "session_handoff", False) or hub_bot is None:
        _answer(service, callback.callback_id, "Кнопка новой сессии выключена в настройках Hub")
        return True
    if callback.data == HANDOFF_DONE_CALLBACK:
        _answer(service, callback.callback_id, USED)
        return True
    parsed = parse_handoff_callback(callback.data)
    if parsed is None or callback.message_sender_id != hub_bot.telegram_user_id:
        _answer(service, callback.callback_id, STALE)
        return True
    index, binding = parsed
    state = service.state
    control = _delivered_control(state, callback)
    topic = state.find_topic(callback.chat_id, callback.thread_id)
    if (
        control is None
        or topic is None
        or topic.topic_id != topic_id
        or topic.project_id != project_id
        or handoff_binding(control[0]) != binding
        or index > len(control[1])
    ):
        _answer(service, callback.callback_id, STALE)
        return True
    job_id, labels = control
    try:
        job = state.get_provider_job(job_id)
    except StateError:
        _answer(service, callback.callback_id, STALE)
        return True
    saved = _saved_answer(state, job_id)
    extracted = extract_handoff(saved) if saved is not None else None
    if job.topic_id != topic_id or extracted is None or extracted[1] != labels:
        _answer(service, callback.callback_id, STALE)
        return True
    body = extracted[0]
    label = labels[index - 1] if index else None
    record = f"handoff:{callback.chat_id}:{callback.message_id}"
    # A committed press is recognized before any session check, so a repeat
    # after a crash reports the first use instead of a stale session.
    if state.message_already_observed(callback.chat_id, callback.message_id) or _recorded(
        state, record
    ):
        _answer(service, callback.callback_id, USED)
        return True
    refusal = _session_refusal(state, topic_id, job)
    if refusal is not None or not fits(body, labels):
        _answer(service, callback.callback_id, refusal or STALE)
        return True
    active = state.active_session(topic_id)
    text = request_text(body, label)
    message = TopicMessage(
        update_id=0,
        # The pressed Hub message id is unique in the chat and never a user
        # message, so durable admission is idempotent per pressed control.
        message_id=callback.message_id,
        chat_id=callback.chat_id,
        thread_id=callback.thread_id,
        chat_title=project_id,
        sender_id=callback.sender_id,
        text=text,
    )
    result = controller_provider_admission(service, message, active).admit(
        DurableAdmissionRequest(
            message=message, topic=topic, session=active, prompt=text, rotate_session=True
        )
    )
    if isinstance(result, DuplicateAdmission):
        _answer(service, callback.callback_id, USED)
        return True
    if isinstance(result, RejectedAdmission) and result.reason == "session_changed":
        # Nothing committed: the button stays usable once the session settles.
        _answer(service, callback.callback_id, "Сессия как раз изменилась, нажми ещё раз")
        return True
    if not isinstance(result, (CommittedAdmission, RejectedAdmission)):
        _answer(service, callback.callback_id, "Hub не смог принять нажатие, попробуй ещё раз")
        return False
    admitted = isinstance(result, CommittedAdmission)
    if not admitted and not service._uses_external_outbox_sender():
        service._deliver_embedded_blocker_notice(state)
    state.claim_callback(
        record, observer_agent_id=getattr(service, "ingress_identity", service.agent.agent_id)
    )
    try:
        service.telegram.call(
            "editMessageReplyMarkup",
            chat_id=callback.chat_id,
            message_id=callback.message_id,
            reply_markup=json.dumps(
                done_keyboard(label)
                if admitted
                else {
                    "inline_keyboard": [
                        [{"text": "✗ Hub не принял", "callback_data": HANDOFF_DONE_CALLBACK}]
                    ]
                },
                ensure_ascii=False,
            ),
        )
        service.telegram.answer_callback(
            callback.callback_id,
            "Новая сессия начата" if admitted else "Hub не принял сообщение, причина — в теме",
        )
    except TelegramError as error:
        # The press is already settled; a repeat is refused by the record.
        survived("session_handoff.markup", error)
    return True


def _session_refusal(state: Any, topic_id: int, job: Any) -> str | None:
    """Plain reasons why this press cannot replace the session now, or None."""
    active = state.active_session(topic_id)
    if active is None or active.session_id != job.session_id:
        return "Кнопка относится к прошлой сессии: в теме уже другая сессия"
    later = state._connection.execute(
        "SELECT 1 FROM provider_jobs WHERE session_id = ? AND topic_sequence > ? LIMIT 1",
        (job.session_id, job.topic_sequence),
    ).fetchone()
    if later is not None:
        return "После этого ответа в сессии уже была работа, кнопка устарела"
    if active.writer_mode != "telegram":
        return "Сессией сейчас владеет локальный терминал: верни её в Telegram и нажми снова"
    if state.topic_has_running_dispatch(topic_id) or state.topic_has_pending_provider_job(topic_id):
        return "Идёт ход или доставка ответа: дождись окончания и нажми снова"
    return None


def _delivered_control(state: Any, callback: TopicCallback) -> tuple[str, tuple[str, ...]] | None:
    """The source job and options of the exact delivered handoff control."""
    rows = state._connection.execute(
        """SELECT outbox.job_id, parts.telegram_html FROM telegram_outbox_parts parts
           JOIN telegram_outbox outbox ON outbox.outbox_id = parts.outbox_id
           WHERE outbox.chat_id = ? AND outbox.thread_id = ?
             AND parts.telegram_message_id = ? AND parts.delivered_at IS NOT NULL""",
        (callback.chat_id, callback.thread_id, callback.message_id),
    ).fetchall()
    controls = [
        (str(row[0]), parsed[0])
        for row in rows
        if (parsed := parse_handoff_part(str(row[1]))) is not None and parsed[1]
    ]
    # Missing or ambiguous delivery evidence refuses; there is no fallback.
    return controls[0] if len(controls) == 1 else None


def _saved_answer(state: Any, job_id: str) -> str | None:
    row = state._connection.execute(
        "SELECT visible_response FROM provider_job_results WHERE job_id = ?", (job_id,)
    ).fetchone()
    return None if row is None else str(row[0])


def _recorded(state: Any, record: str) -> bool:
    return (
        state._connection.execute(
            "SELECT 1 FROM observed_callbacks WHERE callback_id = ?", (record,)
        ).fetchone()
        is not None
    )


def _answer(service: Any, callback_id: str, text: str) -> None:
    try:
        service.telegram.answer_callback(callback_id, text)
    except TelegramError as error:
        survived("session_handoff.markup", error)


__all__ = ["handle_session_handoff"]
