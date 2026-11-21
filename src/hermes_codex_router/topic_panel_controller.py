"""Owner `/panel` commands and the read-only «Обновить» callback (ADR 0110).

Both are control-only: they never become provider prompts, import materials,
or change jobs, queues, holds, writers or deliveries. With the local
``topic_panel`` flag off the command keeps its previous routing, and every
panel callback is refused without a state change.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .routing import parse_command
from .telegram import TopicCallback, TopicMessage
from .topic_panel_state import TopicPanelStore
from .topic_panel_view import PANEL_REFRESH_PREFIX

PANEL_CALLBACK_PREFIX = "pn:"
PANEL_STALE = "Панель устарела; откройте /panel"
PANEL_REFRESHING = "Проверяю. Если состояние изменилось, панель обновится."
PANEL_USAGE = "Используйте /panel или /panel off без вложений."
_COMMAND_BOT = re.compile(r"/[A-Za-z][A-Za-z0-9_]*(?:@([A-Za-z][A-Za-z0-9_]*))?")
_REFRESH = re.compile(re.escape(PANEL_REFRESH_PREFIX) + r"([1-9][0-9]{0,17})")


def try_handle_topic_panel_command(service: Any, message: TopicMessage) -> bool | None:
    """Handle `/panel` before routing; None keeps the previous behavior."""
    command = parse_command(message.text)
    config = service.config
    if not config.topic_panel or command is None or command.name != "panel":
        return None
    addressed = _COMMAND_BOT.match(message.text.strip())
    hub_username = config.hub_bot.telegram_username if config.hub_bot is not None else ""
    if message.is_forwarded or (
        addressed is not None
        and addressed.group(1) is not None
        and addressed.group(1).casefold() != hub_username.casefold()
    ):
        return None  # A forward stays a quote; another bot's command keeps its routing.
    ingress = getattr(service, "ingress_identity", service.agent.agent_id)
    if ingress != "hub":
        return False  # Only the central Hub ingress claims and serves panel commands.
    if not service.state.claim_message(
        message.chat_id, message.message_id, observer_agent_id=ingress
    ):
        return False
    if message.sender_id not in service.config.owner_user_ids:
        service._send_text(message, "Панель может открыть только владелец.")
        return True
    if (
        message.attachments
        or message.unavailable_materials
        or command.arguments
        not in (
            (),
            ("off",),
        )
    ):
        service._send_text(message, PANEL_USAGE)
        return True
    from .service import ServiceError  # Late: the Controller imports this module.

    try:
        if message.chat_id == message.sender_id:
            raise KeyError(message.chat_id)
        binding = service._project_binding_for_chat(message.chat_id)
    except (KeyError, ServiceError):
        service._send_text(message, "Панель доступна только в теме проекта.")
        return True
    topic = service._topic(message, binding.project_id)
    store = TopicPanelStore(service.state)
    now = datetime.now(timezone.utc)
    if command.arguments == ("off",):
        closed = store.close(topic.topic_id, now=now)
        service._send_text(
            message,
            "Панель остановлена и больше не обновляется."
            if closed
            else "В этой теме нет открытой панели.",
        )
        return True
    store.open(topic, now=now)  # The panel message itself is the answer.
    return True


def handle_topic_panel_callback(service: Any, callback: TopicCallback) -> bool:
    """Serve one claimed `pn:` press for the exact current panel, or refuse it."""
    config = service.config
    if callback.sender_id not in config.owner_user_ids:
        service.telegram.answer_callback(
            callback.callback_id, "Обновить панель может только владелец"
        )
        return True
    hub_bot_id = config.hub_bot.telegram_user_id if config.hub_bot is not None else None
    match = _REFRESH.fullmatch(callback.data)
    if (
        not config.topic_panel
        or match is None
        or hub_bot_id is None
        or callback.message_sender_id != hub_bot_id
    ):
        service.telegram.answer_callback(callback.callback_id, PANEL_STALE)
        return True
    outcome = TopicPanelStore(service.state).request_refresh(
        chat_id=callback.chat_id,
        thread_id=callback.thread_id,
        message_id=callback.message_id,
        instance=int(match.group(1)),
        now=datetime.now(timezone.utc),
    )
    service.telegram.answer_callback(
        callback.callback_id, PANEL_STALE if outcome == "stale" else PANEL_REFRESHING
    )
    return True
