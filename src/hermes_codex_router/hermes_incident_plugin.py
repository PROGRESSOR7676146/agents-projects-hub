"""Default-off incident presentation in Hermes' supported PTB JobQueue lifecycle.

No adapter internals, Hub database/config/token access, model turn, approval or
update action. Application start/stop owns the timer and all its callbacks.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import weakref
from pathlib import Path
from typing import Any

from .diagnostic_log import configure_process_logging, survived
from .hermes_incident_cursor import NoticeSettings, _pairs
from .hermes_incident_notices import HermesIncidentNotices, file_call
from .incident_journal import IncidentJournalError, _read

CONFIG_ENV = "HERMES_PROJECT_HUB_INCIDENT_NOTICES_CONFIG"
_KEY = "hub_incident_notices_v1"
_INTEGRATIONS: weakref.WeakKeyDictionary[Any, _Integration] = weakref.WeakKeyDictionary()
_HIDE = re.compile(r"hubinc:hide:([0-9a-f]{32})\Z")


def _load(path: Path) -> tuple[NoticeSettings, str]:
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise ValueError("incident_notice_configuration_invalid")
    raw = _read(path, 16384)
    value = json.loads(raw, object_pairs_hook=_pairs)
    required = {"schema_version", "journal", "cursor", "owner"}
    optional = {"interval", "monitor_interval", "max_cards", "window_seconds"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - optional
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("incident_notice_configuration_invalid")
    for name in ("journal", "cursor"):
        if not isinstance(value[name], str) or not 0 < len(value[name]) <= 4096:
            raise ValueError("incident_notice_configuration_invalid")
    settings = NoticeSettings(
        Path(value["journal"]),
        Path(value["cursor"]),
        value["owner"],
        **{key: value[key] for key in optional if key in value},
    )
    # The opt-in itself must not be overwritten by a cursor or its lock.
    if path in {
        settings.cursor,
        settings.cursor.with_suffix(".lock"),
        settings.journal,
        settings.journal.with_suffix(".lock"),
        settings.journal.with_suffix(".seed"),
    }:
        raise ValueError("incident_notice_configuration_invalid")
    return settings, hashlib.sha256(raw).hexdigest()


class _Integration:
    def __init__(
        self, application: Any, raw_path: str, settings: NoticeSettings, digest: str
    ) -> None:
        self.application = weakref.proxy(application)
        self.raw_path, self.path, self.digest = raw_path, Path(raw_path), digest
        self.consumer = HermesIncidentNotices(settings)
        self.retired = False
        self.job: Any = None

    def _current(self) -> bool:
        if self.retired:
            return False
        try:
            if (
                os.getenv(CONFIG_ENV) != self.raw_path
                or hashlib.sha256(_read(self.path, 16384)).hexdigest() != self.digest
            ):
                raise ValueError("incident_notice_configuration_changed")
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
            IncidentJournalError,
        ) as error:
            self.retired = True
            survived("hermes_plugin.incident_retired", error)
            return False
        return True

    def _remove(self) -> None:
        if self.job is not None:
            self.job.schedule_removal()

    async def send(self, text: str, token: str) -> object:
        if not await file_call(self._current):
            self._remove()
            # No network request, but preserve the pre-existing attempted fence.
            raise RuntimeError("incident_notices_retired")
        telegram = importlib.import_module("telegram")
        owner = self.consumer.settings.owner
        markup = (
            telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("Скрыть", callback_data=f"hubinc:hide:{token}")]]
            )
            if await file_call(self.consumer.has_open_members, token)
            else None
        )
        # PTB's error classes do not retain validated rejection-body proof:
        # BadRequest/Forbidden may follow malformed or contradictory HTTP bodies,
        # and RetryAfter may follow any non-2xx status with retry parameters.
        # Leave all native SDK failures unknown; never infer safe resend from
        # class names or exception text. NoticeRejected is reserved for transports
        # with independent proof, exercised by the engine's offline fixtures.
        message = await self.application.bot.send_message(
            chat_id=owner,
            text=text,
            parse_mode=None,
            reply_markup=markup,
            disable_web_page_preview=True,
            read_timeout=2,
            write_timeout=2,
            connect_timeout=1,
            pool_timeout=1,
        )
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if type(chat_id) is not int or chat_id != owner or getattr(chat, "type", None) != "private":
            raise RuntimeError("incident_notice_receipt_invalid")
        return getattr(message, "message_id", None)

    async def tick(self, context: Any) -> None:
        if not await file_call(self._current):
            self._remove()
            return
        outcome = await self.consumer.tick(self.send)
        # Literal closed labels; the logger never sees journal/card/SDK error.
        if outcome == "unavailable":
            survived("hermes_plugin.incident_unavailable", RuntimeError())
        elif outcome == "unknown":
            survived("hermes_plugin.incident_unknown", RuntimeError())
        elif outcome == "rejected":
            survived("hermes_plugin.incident_rejected", RuntimeError())

    async def control(self, update: Any, context: Any) -> None:
        stop = importlib.import_module("telegram.ext").ApplicationHandlerStop
        query = update.callback_query
        accepted = False
        try:
            match = _HIDE.fullmatch(query.data) if isinstance(query.data, str) else None
            user, message = query.from_user, query.message
            chat = getattr(message, "chat", None)
            if (
                match is not None
                and getattr(user, "is_bot", None) is False
                and chat is not None
                and getattr(chat, "type", None) == "private"
                and await file_call(self._current)
            ):
                accepted = await file_call(
                    self.consumer.dismiss,
                    match.group(1),
                    user.id,
                    chat.id,
                    message.message_id,
                )
            if self.retired:
                self._remove()
            # Acknowledges only this deterministic control, never a provider turn.
            async with asyncio.timeout(3):
                await query.answer(
                    "Скрыто: уведомление о закрытии подавлено."
                    if accepted
                    else "Карточка недоступна.",
                    read_timeout=2,
                    write_timeout=2,
                    connect_timeout=1,
                    pool_timeout=1,
                )
        except Exception as error:
            survived("hermes_plugin.incident_control", error)
        # Even stale/foreign/malformed own controls cannot reach core's
        # catch-all callback handler or any native model dispatcher.
        raise stop


def install_incident_notices(application: Any) -> bool:
    raw = os.getenv(CONFIG_ENV, "")
    if not raw:
        return False
    configure_process_logging()
    if application in _INTEGRATIONS:
        return True
    try:
        settings, digest = _load(Path(raw))
        telegram = importlib.import_module("telegram")
        if telegram.__version__ != "22.8" or application.job_queue is None:
            raise ValueError("incident_notice_lifecycle_unavailable")
        ext = importlib.import_module("telegram.ext")
        integration = _Integration(application, raw, settings, digest)
        # This native scheduler is started/stopped by Application, including
        # disconnect/reconnect. No post hooks, create_task loop or new service.
        integration.job = application.job_queue.run_repeating(
            integration.tick,
            interval=settings.interval,
            first=1,
            name=_KEY,
            job_kwargs={"max_instances": 1, "coalesce": True},
        )
        try:
            application.add_handler(
                ext.CallbackQueryHandler(integration.control, pattern=r"^hubinc:", block=True),
                group=-30,
            )
        except Exception:
            integration._remove()
            raise
        _INTEGRATIONS[application] = integration
        return True
    except Exception as error:
        survived("hermes_plugin.incident_registration", error)
        return False
