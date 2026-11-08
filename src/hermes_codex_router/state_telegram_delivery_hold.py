"""HubState transaction facade for one Hub-Bot delivery-hold callback.

The domain helper owns the exact snapshot and ledger rules.  This facade keeps
the established HubState connection and transaction ownership without growing
the general state facade with a second orchestration responsibility.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import ContextManager, Sequence

from .state_errors import StateError
from .telegram_delivery_hold_state import (
    TelegramDeliveryHoldDisposition,
    TelegramDeliveryHoldPreview,
    TelegramDeliveryHoldState,
)


class TelegramDeliveryHoldHubStateFacade:
    """Mixin for HubState's existing SQLite transaction boundary."""

    _connection: sqlite3.Connection

    def _immediate_transaction(self) -> ContextManager[None]:
        raise NotImplementedError

    def prepare_telegram_delivery_hold_preview(
        self,
        *,
        owner_user_id: int,
        owner_user_ids: Sequence[int],
        hub_bot_user_id: int,
        chat_id: int,
        thread_id: int,
        notice_message_id: int,
        now: datetime | None = None,
    ) -> TelegramDeliveryHoldPreview:
        """Read one delivered Hub warning into a transient, exact callback preview."""
        if self._connection.in_transaction:
            raise StateError("cannot nest a Telegram delivery-hold preview transaction")
        self._connection.execute("BEGIN")
        try:
            preview = TelegramDeliveryHoldState(self._connection, StateError).prepare_preview(
                owner_user_id=owner_user_id,
                owner_user_ids=owner_user_ids,
                hub_bot_user_id=hub_bot_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                notice_message_id=notice_message_id,
                now=now or datetime.now(timezone.utc),
            )
            self._connection.commit()
            return preview
        except BaseException:
            self._connection.rollback()
            raise

    def bind_telegram_delivery_hold_preview(
        self,
        preview: TelegramDeliveryHoldPreview,
        *,
        owner_user_ids: Sequence[int],
        preview_message_id: int,
        now: datetime | None = None,
    ) -> None:
        """Make a positively returned Hub-Bot preview message usable exactly once."""
        with self._immediate_transaction():
            TelegramDeliveryHoldState(self._connection, StateError).bind_preview_message(
                preview,
                owner_user_ids=owner_user_ids,
                preview_message_id=preview_message_id,
                now=now or datetime.now(timezone.utc),
            )

    def apply_telegram_delivery_hold_callback(
        self,
        token: str,
        *,
        owner_user_id: int,
        owner_user_ids: Sequence[int],
        chat_id: int,
        thread_id: int,
        callback_message_id: int,
        hub_bot_user_id: int,
        callback_id: str,
        callback_origin: str,
        now: datetime | None = None,
    ) -> TelegramDeliveryHoldDisposition:
        """CAS one owner-bound callback; it never changes delivery or execution evidence."""
        with self._immediate_transaction():
            return TelegramDeliveryHoldState(self._connection, StateError).apply_callback(
                token,
                owner_user_id=owner_user_id,
                owner_user_ids=owner_user_ids,
                chat_id=chat_id,
                thread_id=thread_id,
                callback_message_id=callback_message_id,
                hub_bot_user_id=hub_bot_user_id,
                callback_id=callback_id,
                callback_origin=callback_origin,
                now=now or datetime.now(timezone.utc),
            )
