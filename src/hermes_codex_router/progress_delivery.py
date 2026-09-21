from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .state import HubState
from .state_delivery import (
    MAX_PROGRESS_ATTEMPTS,
    PROGRESS_INTERVAL_SECONDS,
    PROGRESS_LEASE_SECONDS,
    ProgressDeliveryRecord,
)

__all__ = [
    "MAX_PROGRESS_ATTEMPTS",
    "PROGRESS_INTERVAL_SECONDS",
    "PROGRESS_LEASE_SECONDS",
    "ProgressDeliveryQueue",
    "ProgressDeliveryRecord",
]


class ProgressDeliveryQueue:
    """Compatibility facade for durable advisory delivery state."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection
        self._delivery_state = state._delivery_state

    def get(self, progress_id: str) -> ProgressDeliveryRecord:
        return self._delivery_state.get_progress(progress_id)

    def for_job(self, job_id: str) -> tuple[ProgressDeliveryRecord, ...]:
        return self._delivery_state.progress_for_job(job_id)

    def enqueue(
        self,
        job_id: str,
        lease_token: str,
        item_sequence: int,
        text: str,
        *,
        now: datetime | None = None,
        min_interval_seconds: int = PROGRESS_INTERVAL_SECONDS,
    ) -> bool:
        return self._delivery_state.enqueue_progress(
            job_id,
            lease_token,
            item_sequence,
            text,
            now=now,
            min_interval_seconds=min_interval_seconds,
        )

    def enqueue_in_transaction(
        self,
        job_id: str,
        lease_token: str,
        item_sequence: int,
        text: str,
        *,
        now: datetime | None = None,
        min_interval_seconds: int = PROGRESS_INTERVAL_SECONDS,
    ) -> bool:
        return self._delivery_state.enqueue_progress_in_transaction(
            job_id,
            lease_token,
            item_sequence,
            text,
            now=now,
            min_interval_seconds=min_interval_seconds,
        )

    def recover_stale(self, sender_agent_ids: Sequence[str], *, now: datetime | None = None) -> int:
        return self._delivery_state.recover_stale_progress(sender_agent_ids, now=now)

    def supersede_terminal(
        self, sender_agent_ids: Sequence[str], *, now: datetime | None = None
    ) -> int:
        return self._delivery_state.supersede_terminal_progress(sender_agent_ids, now=now)

    def lease(
        self,
        sender_agent_id: str,
        lease_owner: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = PROGRESS_LEASE_SECONDS,
    ) -> ProgressDeliveryRecord | None:
        return self._delivery_state.lease_progress(
            sender_agent_id,
            lease_owner,
            now=now,
            lease_seconds=lease_seconds,
        )

    def release(self, progress_id: str, lease_token: str, *, now: datetime | None = None) -> None:
        self._delivery_state.release_progress(progress_id, lease_token, now=now)

    def retry(
        self,
        progress_id: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: float,
        now: datetime | None = None,
    ) -> None:
        self._delivery_state.retry_progress(
            progress_id,
            lease_token,
            error_code=error_code,
            delay_seconds=delay_seconds,
            now=now,
        )

    def mark_delivered(
        self,
        progress_id: str,
        lease_token: str,
        *,
        telegram_message_id: int,
        now: datetime | None = None,
    ) -> None:
        self._delivery_state.mark_progress_delivered(
            progress_id,
            lease_token,
            telegram_message_id=telegram_message_id,
            now=now,
        )
