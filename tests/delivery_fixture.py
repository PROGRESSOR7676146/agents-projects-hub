"""Explicit synthetic send boundary for tests which supply fictional receipts."""

from datetime import datetime

from hermes_codex_router.state import HubState
from hermes_codex_router.state_delivery import TelegramOutboxRecord


def complete_final_delivery(
    state: HubState,
    outbox_id: str,
    lease_token: str,
    *,
    telegram_message_id: int,
    now: datetime | None = None,
) -> TelegramOutboxRecord:
    part = state.delivery.next_outbox_part(outbox_id, lease_token, now=now)
    state.delivery.begin_outbox_send(outbox_id, lease_token, part.part_index, now=now)
    return state.delivery.mark_outbox_delivered(
        outbox_id,
        lease_token,
        part_index=part.part_index,
        telegram_message_id=telegram_message_id,
        now=now,
    )
