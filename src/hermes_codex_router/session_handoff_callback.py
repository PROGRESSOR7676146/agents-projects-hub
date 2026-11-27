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
    DurableAdmissionFailure,
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
from .state_handoff import HandoffAdmission, has_later_handoff_input, is_model_handoff_successor
from .telegram import TelegramError, TopicCallback, TopicMessage

STALE = "Кнопка устарела"
USED = "Кнопка уже использована"


def handle_session_handoff(
    service: Any, callback: TopicCallback, project_id: str, topic_id: int
) -> bool:
    """Start the successor session with the saved answer as its first input, once."""
    hub_bot = service.config.hub_bot
    state = service.state
    if callback.data == HANDOFF_DONE_CALLBACK:
        _answer(service, callback.callback_id, USED)
        return True
    if hub_bot is None or callback.message_sender_id != hub_bot.telegram_user_id:
        _answer(service, callback.callback_id, STALE)
        return True
    record = f"handoff:{callback.chat_id}:{callback.message_id}"
    # A settled press is recognized first, even with the feature now off or
    # after a crash before the keyboard edit: it reports the first use.
    if state.message_already_observed(callback.chat_id, callback.message_id) or _recorded(
        state, record
    ):
        _answer(service, callback.callback_id, USED)
        return True
    if not getattr(service.config, "session_handoff", False):
        _answer(service, callback.callback_id, "Кнопка новой сессии выключена в настройках Hub")
        return True
    parsed = parse_handoff_callback(callback.data)
    control = _delivered_control(state, callback)
    topic = state.find_topic(callback.chat_id, callback.thread_id)
    if (
        parsed is None
        or control is None
        or topic is None
        or topic.topic_id != topic_id
        or topic.project_id != project_id
        or control[2] != parsed[1]
        or parsed[0] > len(control[1])
    ):
        _answer(service, callback.callback_id, STALE)
        return True
    index = parsed[0]
    job_id, labels, _ = control
    try:
        job = state.get_provider_job(job_id)
        source = state.get_session(job.session_id)
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
    target = state.active_session(topic_id)
    refusal = _session_refusal(state, topic_id, job)
    if refusal is not None or target is None or not fits(body, labels):
        _answer(service, callback.callback_id, refusal or STALE)
        return True
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
    after_model = source.session_id != target.session_id
    result = controller_provider_admission(service, message, target).admit(
        DurableAdmissionRequest(
            message=message,
            topic=topic,
            session=target,
            prompt=text,
            session_handoff=HandoffAdmission(source.session_id, job.topic_sequence),
        )
    )
    if isinstance(result, DuplicateAdmission):
        _answer(service, callback.callback_id, USED)
        return True
    admitted = isinstance(result, CommittedAdmission)
    # Only an admitted message or a durably claimed refusal spends the button.
    if isinstance(result, DurableAdmissionFailure):
        _answer(service, callback.callback_id, "Hub не смог принять нажатие, попробуй ещё раз")
        return False
    if after_model and isinstance(result, RejectedAdmission):
        if result.reason == "persistent_root_blocker":
            _answer(
                service, callback.callback_id, "Работа в корне проекта заблокирована. См. /status"
            )
            return True
        if result.reason == "codex_permission_selection_changed":
            _answer(service, callback.callback_id, "Профиль Codex изменился. Открой /new")
            return True
    if not admitted and not state.message_already_observed(callback.chat_id, callback.message_id):
        _answer(service, callback.callback_id, "Сессия как раз изменилась, нажми ещё раз")
        return True
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
            ("Резюме передано" if after_model else "Новая сессия начата")
            if admitted
            else "Hub не принял сообщение, причина — в теме",
        )
    except TelegramError as error:
        # The press is already settled; a repeat is refused by the record.
        survived("session_handoff.markup", error)
    return True


def _session_refusal(state: Any, topic_id: int, job: Any) -> str | None:
    """Plain reasons why this press cannot replace the session now, or None."""
    active = state.active_session(topic_id)
    if active is None or (
        active.session_id != job.session_id
        and not is_model_handoff_successor(
            state._connection, state.get_session(job.session_id), active
        )
    ):
        return "Кнопка относится к прошлой сессии: в теме уже другая сессия"
    if has_later_handoff_input(
        state._connection, topic_id, HandoffAdmission(job.session_id, job.topic_sequence)
    ):
        return "После этого ответа в сессии уже была работа, кнопка устарела"
    if active.writer_mode != "telegram":
        return "Сессией сейчас владеет локальный терминал: верни её в Telegram и нажми снова"
    if state.topic_has_running_dispatch(topic_id) or state.topic_has_pending_provider_job(topic_id):
        return "Идёт ход или доставка ответа: дождись окончания и нажми снова"
    return None


def _delivered_control(
    state: Any, callback: TopicCallback
) -> tuple[str, tuple[str, ...], str] | None:
    """The source job, options and binding of the exact delivered handoff control.

    Exactly one delivered part may carry the pressed message id, it must be the
    last part of its answer (where Hub puts the control) and its stored binding
    must be that job's own binding. Anything else refuses; there is no fallback.
    """
    rows = state._connection.execute(
        """SELECT outbox.job_id, parts.telegram_html,
                  parts.part_index = (SELECT MAX(last.part_index) FROM telegram_outbox_parts last
                                      WHERE last.outbox_id = parts.outbox_id)
           FROM telegram_outbox_parts parts
           JOIN telegram_outbox outbox ON outbox.outbox_id = parts.outbox_id
           WHERE outbox.chat_id = ? AND outbox.thread_id = ?
             AND parts.telegram_message_id = ? AND parts.delivered_at IS NOT NULL""",
        (callback.chat_id, callback.thread_id, callback.message_id),
    ).fetchall()
    if len(rows) != 1 or not rows[0][2]:
        return None
    job_id = str(rows[0][0])
    parsed = parse_handoff_part(str(rows[0][1]))
    if parsed is None or parsed[1] != handoff_binding(job_id):
        return None
    return job_id, parsed[0], parsed[1]


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
