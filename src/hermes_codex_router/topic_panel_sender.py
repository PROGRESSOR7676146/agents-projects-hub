"""One low-priority topic panel effect per external sender cycle (ADR 0110).

The sender reaches this step only after every mandatory delivery and notice
had nothing to do. A panel is checked every ten seconds while its topic has
work and every minute when idle; an unchanged body causes no HTTP request.
Proven panel-only rejections stop the panel without touching shared transport
health; network failures, 401 and 429 still count as shared transport faults.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .diagnostic_log import survived
from .state import HubState
from .telegram import TelegramError, has_native_not_sent_proof
from .topic_panel_snapshot import read_panel_snapshot
from .topic_panel_state import PanelOperation, TopicPanelStore
from .topic_panel_view import (
    panel_body_digest,
    panel_keyboard,
    render_panel_body,
    render_panel_message,
)

PANEL_ACTIVE_SECONDS = 10
PANEL_IDLE_SECONDS = 60
PANEL_SCAN_SECONDS = 1.0  # also the global ceiling of one panel request per second
PANEL_DEFAULT_RETRY_AFTER = 5


class PanelTelegram(Protocol):
    def call(self, method: str, **params: object) -> object: ...

    def send_html(
        self,
        chat_id: int,
        thread_id: int,
        html: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
    ) -> int: ...


class TopicPanelSender:
    def __init__(
        self,
        state: HubState,
        api: PanelTelegram,
        *,
        agent_names: Mapping[str, str],
        transport_success: Callable[[], None],
        transport_failure: Callable[[TelegramError], None],
        record_event: Callable[[str, str, str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = TopicPanelStore(state)
        self._connection = state._connection
        self._api = api
        self._agent_names = dict(agent_names)
        self._transport_success = transport_success
        self._transport_failure = transport_failure
        self._record_event = record_event
        self._clock = clock
        self._next_scan = 0.0
        self._paused_until = 0.0

    def deliver_one(self, *, now: datetime | None = None) -> bool:
        """Return True after one panel HTTP attempt; never more than one."""
        current = now if now is not None else datetime.now(timezone.utc)
        recovered = self.store.recover_stale(now=current)
        if recovered:
            self._record_event("warning", "topic_panel_send_unknown", f"lease_expired={recovered}")
        monotonic = self._clock()
        if monotonic < self._paused_until or monotonic < self._next_scan:
            return False
        self._next_scan = monotonic + PANEL_SCAN_SECONDS
        for panel in self.store.due(now=current):
            try:
                snapshot = self._snapshot(panel.topic_id, current)
                body = render_panel_body(snapshot)
            except Exception as error:
                survived("topic_panel_sender.snapshot", error)
                self.store.postpone(
                    panel, next_due_at=current + timedelta(seconds=PANEL_IDLE_SECONDS), now=current
                )
                continue
            digest = panel_body_digest(body)
            next_due = current + timedelta(
                seconds=(
                    PANEL_IDLE_SECONDS
                    if snapshot.headline in ("idle", "local_writer")
                    else PANEL_ACTIVE_SECONDS
                )
            )
            if panel.message_id is not None and digest == panel.confirmed_digest:
                self.store.defer_unchanged(panel, next_due_at=next_due, now=current)
                continue
            operation = self.store.begin(
                panel,
                payload_html=render_panel_message(body, prepared_at=current),
                reply_markup=panel_keyboard(panel.instance),
                body_digest=digest,
                now=current,
            )
            if operation is None:
                continue
            self._send(operation, next_due=next_due, now=current)
            return True
        return False

    def _snapshot(self, topic_id: int, now: datetime):
        # One deferred read transaction: every query sees the same snapshot,
        # and no write lock is taken for an unchanged panel.
        self._connection.execute("BEGIN")
        try:
            return read_panel_snapshot(
                self._connection, topic_id=topic_id, now=now, agent_names=self._agent_names
            )
        finally:
            self._connection.rollback()

    def _send(self, operation: PanelOperation, *, next_due: datetime, now: datetime) -> None:
        try:
            if operation.kind == "create":
                message_id = self._api.send_html(
                    operation.chat_id,
                    operation.thread_id,
                    operation.payload_html,
                    reply_markup=operation.reply_markup,
                    disable_notification=True,
                )
            else:
                result = self._api.call(
                    "editMessageText",
                    chat_id=operation.chat_id,
                    message_id=operation.target_message_id,
                    text=operation.payload_html,
                    parse_mode="HTML",
                    disable_web_page_preview="true",
                    reply_markup=json.dumps(operation.reply_markup, ensure_ascii=False),
                )
                target = operation.target_message_id
                if (
                    target is None
                    or not isinstance(result, dict)
                    or result.get("message_id") != target
                ):
                    raise TelegramError(
                        "editMessageText returned an invalid result",
                        operation="api_call",
                        failure_class="invalid_response",
                    )
                message_id = target
        except TelegramError as error:
            self._settle_error(operation, error, next_due=next_due, now=now)
            return
        except Exception as error:
            survived("topic_panel_sender.send", error)
            self.store.mark_unknown(operation, error_code="unexpected_client", now=now)
            self._record_event("warning", "topic_panel_send_unknown", f"kind={operation.kind}")
            return
        self.store.confirm(operation, receipt_message_id=message_id, next_due_at=next_due, now=now)
        self._transport_success()

    def _settle_error(
        self,
        operation: PanelOperation,
        error: TelegramError,
        *,
        next_due: datetime,
        now: datetime,
    ) -> None:
        status = error.status_code
        rejected = error.failure_class == "api_rejection" and status is not None
        if (
            rejected
            and error.message_not_modified
            and operation.kind == "edit"
            and operation.target_message_id is not None
        ):
            # Exact target and payload: the message already shows this version.
            self.store.confirm(
                operation,
                receipt_message_id=operation.target_message_id,
                next_due_at=next_due,
                now=now,
            )
            return
        if rejected and status == 429:
            delay = error.retry_after if error.retry_after else PANEL_DEFAULT_RETRY_AFTER
            self._paused_until = self._clock() + delay
            self.store.reject(
                operation,
                error_code="api_429",
                now=now,
                retry_at=now + timedelta(seconds=delay),
            )
            self.store.defer_all(until=now + timedelta(seconds=delay), now=now)
            self._transport_failure(error)
            return
        if rejected and status == 401:
            self.store.reject(
                operation,
                error_code="api_401",
                now=now,
                retry_at=now + timedelta(seconds=PANEL_IDLE_SECONDS),
            )
            self._transport_failure(error)
            return
        if rejected and status is not None and 400 <= status < 500:
            # Lost rights, a deleted topic or message, or an invalid request:
            # stop this panel instead of looping; shared health is unaffected.
            self.store.reject(operation, error_code=f"api_{status}", now=now, retry_at=None)
            self._record_event(
                "warning", "topic_panel_rejected", f"kind={operation.kind};status={status}"
            )
            return
        if has_native_not_sent_proof(error):
            self.store.reject(
                operation,
                error_code="not_sent",
                now=now,
                retry_at=now + timedelta(seconds=PANEL_ACTIVE_SECONDS),
            )
            self._transport_failure(error)
            return
        self.store.mark_unknown(operation, error_code=error.failure_class, now=now)
        self._transport_failure(error)
        self._record_event(
            "warning",
            "topic_panel_send_unknown",
            f"kind={operation.kind};class={error.failure_class}",
        )


def panel_agent_names(agents: Any) -> dict[str, str]:
    return {agent.agent_id: agent.display_name for agent in agents}
