"""Controller handling of owner decision buttons on Hub-bot messages.

``bh:`` confirms or cancels one held request. ``ud:`` decides one uncertain
non-Codex job (ADR 0107). Both run before the generic callback claim: the
state layer binds each press to its delivered message and makes a repeated
press return the decision already taken, without new effects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .diagnostic_log import survived
from .state import StateError
from .telegram import TelegramError, TopicCallback
from .uncertain_decision import DONE_CALLBACK, done_keyboard, parse_callback
from .uncertain_decision_state import UncertainDecision, UncertainDecisionState

OWNER_DECISION_PREFIXES = ("bh:", "ud:")


def handle_owner_decision_callback(service: Any, callback: TopicCallback) -> bool:
    if callback.sender_id not in service.config.owner_user_ids:
        service.telegram.answer_callback(
            callback.callback_id,
            "Только владелец может решить судьбу запроса"
            if callback.data.startswith("bh:")
            else "Только владелец может решить судьбу задачи",
        )
        return True
    if callback.data.startswith("bh:"):
        return _held(service, callback)
    return _uncertain(service, callback)


def _held(service: Any, callback: TopicCallback) -> bool:
    try:
        _, action, job_id = callback.data.split(":", 2)
        if action not in {"c", "x"}:
            raise StateError("unknown held-job decision")
        decision = service.state.decide_held_provider_job(
            job_id=job_id,
            action="confirm" if action == "c" else "cancel",
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            notice_message_id=callback.message_id,
        )
    except (ValueError, StateError) as exc:
        service.telegram.answer_callback(callback.callback_id, str(exc)[:180])
        return True
    service.telegram.answer_callback(
        callback.callback_id,
        "Запуск подтверждён; запрос ожидает своей очереди"
        if decision == "confirmed"
        else "Запрос отменён",
    )
    return True


def _uncertain(service: Any, callback: TopicCallback) -> bool:
    if callback.data == DONE_CALLBACK:
        _answer(service, callback, "Решение уже принято")
        return True
    parsed = parse_callback(callback.data)
    hub_bot = service.config.hub_bot
    hub_bot_id = hub_bot.telegram_user_id if hub_bot is not None else None
    if (
        parsed is None
        or not service.config.hub_uncertain_decision_enabled
        or callback.message_sender_id is None
        or callback.message_sender_id != hub_bot_id
    ):
        _answer(service, callback, "Кнопка недоступна; паузу снимает локальная команда")
        return True
    action, snapshot = parsed
    try:
        project = service.config.project_for_chat(callback.chat_id)
        root = Path(service.registry.require_project(project.project_id).root)
        decision = UncertainDecisionState(service.state).decide(
            action=action,
            snapshot=snapshot,
            actor_user_id=callback.sender_id,
            callback_id=callback.callback_id,
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            prompt_message_id=callback.message_id,
            canonical_root=root,
        )
    except StateError as exc:
        _answer(service, callback, str(exc)[:180])
        return True
    except (KeyError, ValueError, OSError):
        _answer(service, callback, "Проект этой темы не найден; кнопка не применена")
        return True
    try:
        service.telegram.call(
            "editMessageReplyMarkup",
            chat_id=callback.chat_id,
            message_id=callback.message_id,
            reply_markup=json.dumps(done_keyboard(decision.action), ensure_ascii=False),
        )
    except TelegramError as error:
        # The decision is committed; a stale keyboard returns it again.
        survived("uncertain_decision.markup", error)
    _answer(service, callback, _outcome(decision))
    return True


def _outcome(decision: UncertainDecision) -> str:
    if not decision.created:
        return (
            "Решение уже принято: проверка и продолжение"
            if decision.action == "continue"
            else "Решение уже принято: закрыто без повтора"
        )
    if decision.action == "close":
        return "Закрыто без повтора; проект снят с паузы"
    held = f"; запросов на паузе: {decision.held_count}" if decision.held_count else ""
    return "Проверка и продолжение поставлены в очередь" + held


def _answer(service: Any, callback: TopicCallback, text: str) -> None:
    # An expired query ("query is too old") must not fail the press handling.
    try:
        service.telegram.answer_callback(callback.callback_id, text)
    except TelegramError as error:
        survived("uncertain_decision.markup", error)
