"""Monitor-owned health publication and durable passive Telegram probe streaks."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .runtime_health import MONITOR_INSTANCE_ID
from .state import HubState
from .telegram import TELEGRAM_HEALTH_FAILURE_THRESHOLD, TelegramError


class MonitorHealth:
    def __init__(self, state: HubState, *, started: datetime, marker: str) -> None:
        self.state = state
        self.started = started
        self.marker = marker
        previous = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
        self.failures = 0 if previous is None else previous.transport_consecutive_failures
        self.operation = None if previous is None else previous.transport_operation
        self.failure_class = None if previous is None else previous.transport_failure_class
        self.status_code = None if previous is None else previous.transport_status_code
        self.retry_after = None if previous is None else previous.transport_retry_after
        self.last_success = (
            datetime.fromisoformat(previous.transport_success_at)
            if previous is not None and previous.transport_success_at is not None
            else None
        )

    def observe(self, errors: list[TelegramError], *, checked: bool) -> None:
        """One failed cycle is one failure, regardless of group or bot count."""
        previous_failures = self.failures
        if errors:
            error = errors[0]
            self.failures = min(1_000_000, self.failures + 1)
            self.operation, self.failure_class = error.operation, error.failure_class
            self.status_code, self.retry_after = error.status_code, error.retry_after
            if self.failures == TELEGRAM_HEALTH_FAILURE_THRESHOLD:
                self.state.record_runtime_event(
                    "monitor",
                    "error",
                    "telegram_transport_error",
                    error.safe_detail(
                        consecutive_failures=self.failures,
                        last_success=None
                        if self.last_success is None
                        else self.last_success.isoformat(),
                    ),
                )
        else:
            # Removing all configured probes retires the condition; it does not
            # prove Telegram recovery or create a new success timestamp.
            self.failures = 0
            self.operation = self.failure_class = None
            self.status_code = self.retry_after = None
            if checked:
                self.last_success = datetime.now(timezone.utc)
                if previous_failures >= TELEGRAM_HEALTH_FAILURE_THRESHOLD:
                    self.state.record_runtime_event(
                        "monitor",
                        "info",
                        "telegram_recovered",
                        "Passive Telegram group checks succeeded.",
                    )

    def publish(self, *, success_at: datetime | None = None, error_code: str | None = None) -> None:
        self.state.upsert_runtime_health(
            component="monitor",
            instance_id=MONITOR_INSTANCE_ID,
            pid=os.getpid(),
            process_start_marker=self.marker,
            started_at=self.started,
            heartbeat_at=datetime.now(timezone.utc),
            success_at=success_at,
            error_code=error_code,
            transport_operation=self.operation,
            transport_failure_class=self.failure_class,
            transport_status_code=self.status_code,
            transport_retry_after=self.retry_after,
            transport_consecutive_failures=self.failures,
            transport_success_at=self.last_success,
        )
