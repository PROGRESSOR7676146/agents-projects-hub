from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .telegram import TelegramBotApi


def _publish(telegram: TelegramBotApi, chat_id: int, thread_id: int, message_id: int) -> None:
    telegram.send_chat_action(chat_id, thread_id)
    if chat_id > 0:
        try:
            telegram.send_message_draft(chat_id, thread_id, draft_id=message_id)
        except Exception:
            pass


@contextmanager
def telegram_activity(
    telegram: TelegramBotApi,
    *,
    chat_id: int,
    thread_id: int,
    message_id: int,
) -> Iterator[None]:
    """Refresh native Telegram activity while one inline provider turn runs."""

    stop = threading.Event()
    interval = 4.0
    worker: threading.Thread | None = None

    def refresh() -> None:
        while not stop.wait(interval):
            try:
                _publish(telegram, chat_id, thread_id, message_id)
            except Exception:
                pass

    try:
        try:
            _publish(telegram, chat_id, thread_id, message_id)
        except Exception:
            pass
        worker = threading.Thread(target=refresh, name="telegram-activity", daemon=True)
        worker.start()
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=1)
