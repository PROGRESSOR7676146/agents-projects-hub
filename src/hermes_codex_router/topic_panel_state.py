"""Read-only topic status panel lifecycle and send evidence (ADR 0110).

Every ``/panel`` starts a new panel instance; ``/panel off`` ends the current
one. The external sender commits each create or edit as a ``sending``
operation before any HTTP request and settles it afterwards. Every panel
change caused by an operation is fenced by the panel instance and the exact
target message, so a late receipt never revives a replaced or disabled panel.
An unknown outcome is never retried: it stops the current instance until the
owner opens a new one. Panel rows never authorize provider, queue, delivery or
stop effects.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from .state import HubState, TopicRecord

PANEL_DUE_BATCH = 20
PANEL_SEND_LEASE_SECONDS = 60  # far above the 8 s Bot API request timeout
PANEL_SETTLED_RETENTION = 20

RefreshOutcome = Literal["accepted", "pending", "stale"]

# Parameters: served request, next due time, served request. Clears only the
# served request; a newer press keeps the panel due immediately.
_SERVED_REFRESH = (
    "next_due_at=CASE WHEN refresh_requested_at IS NULL OR refresh_requested_at IS ? "
    "THEN ? ELSE next_due_at END,"
    "refresh_requested_at=CASE WHEN refresh_requested_at IS ? "
    "THEN NULL ELSE refresh_requested_at END"
)


def panel_time(value: datetime) -> str:
    """Store one comparable UTC form for every panel timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("panel time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class PanelRecord:
    topic_id: int
    chat_id: int
    thread_id: int
    instance: int
    message_id: int | None
    confirmed_digest: str | None
    refresh_requested_at: str | None


@dataclass(frozen=True, slots=True)
class PanelOperation:
    operation_id: str
    topic_id: int
    instance: int
    kind: Literal["create", "edit"]
    chat_id: int
    thread_id: int
    target_message_id: int | None
    payload_html: str
    reply_markup: dict[str, Any]
    body_digest: str
    lease_token: str
    # The refresh request this check serves; a newer press stays pending.
    served_refresh: str | None


class TopicPanelStore:
    """Short immediate transactions over panel-owned tables only."""

    def __init__(self, state: HubState) -> None:
        self._state = state
        self._connection = state._connection

    def open(self, topic: TopicRecord, *, now: datetime) -> int:
        """Start a new instance; it is never skipped as unchanged."""
        at = panel_time(now)
        with self._state._immediate_transaction():
            row = self._connection.execute(
                "SELECT instance FROM topic_panels WHERE topic_id=?", (topic.topic_id,)
            ).fetchone()
            instance = 1 if row is None else int(row["instance"]) + 1
            self._connection.execute(
                """INSERT INTO topic_panels(topic_id,chat_id,thread_id,instance,enabled,
                       message_id,confirmed_digest,confirmed_at,confirmed_operation_id,
                       next_due_at,refresh_requested_at,unavailable_reason,updated_at)
                   VALUES(?,?,?,?,1,NULL,NULL,NULL,NULL,?,NULL,NULL,?)
                   ON CONFLICT(topic_id) DO UPDATE SET instance=excluded.instance,enabled=1,
                       message_id=NULL,confirmed_digest=NULL,confirmed_at=NULL,
                       confirmed_operation_id=NULL,
                       next_due_at=excluded.next_due_at,refresh_requested_at=NULL,
                       unavailable_reason=NULL,updated_at=excluded.updated_at""",
                (topic.topic_id, topic.chat_id, topic.thread_id, instance, at, at),
            )
        return instance

    def close(self, topic_id: int, *, now: datetime) -> bool:
        """End the current instance; its message and button become stale."""
        with self._state._immediate_transaction():
            cursor = self._connection.execute(
                """UPDATE topic_panels SET instance=instance+1,enabled=0,
                       refresh_requested_at=NULL,updated_at=?
                   WHERE topic_id=? AND enabled=1""",
                (panel_time(now), topic_id),
            )
        return cursor.rowcount == 1

    def request_refresh(
        self, *, chat_id: int, thread_id: int, message_id: int, instance: int, now: datetime
    ) -> RefreshOutcome:
        """Make the exact current panel due now; a stale binding changes nothing."""
        at = panel_time(now)
        with self._state._immediate_transaction():
            row = self._connection.execute(
                """SELECT topic_id,refresh_requested_at FROM topic_panels
                   WHERE chat_id=? AND thread_id=? AND instance=? AND message_id=?
                     AND enabled=1 AND unavailable_reason IS NULL""",
                (chat_id, thread_id, instance, message_id),
            ).fetchone()
            if row is None:
                return "stale"
            if row["refresh_requested_at"] is not None:
                return "pending"
            self._connection.execute(
                """UPDATE topic_panels SET refresh_requested_at=?,next_due_at=?,updated_at=?
                   WHERE topic_id=?""",
                (at, at, at, row["topic_id"]),
            )
        return "accepted"

    def recover_stale(self, *, now: datetime) -> int:
        """Settle abandoned sends as unknown and stop their instance; never resend."""
        at = panel_time(now)
        stale = self._connection.execute(
            """SELECT 1 FROM topic_panel_operations
               WHERE state='sending' AND lease_expires_at<=? LIMIT 1""",
            (at,),
        ).fetchone()
        if stale is None:
            return 0
        with self._state._immediate_transaction():
            rows = self._connection.execute(
                """SELECT operation_id,topic_id,instance,target_message_id
                   FROM topic_panel_operations
                   WHERE state='sending' AND lease_expires_at<=?""",
                (at,),
            ).fetchall()
            for row in rows:
                self._connection.execute(
                    """UPDATE topic_panel_operations
                       SET state='unknown',error_code='lease_expired',completed_at=?
                       WHERE operation_id=? AND state='sending'""",
                    (at, row["operation_id"]),
                )
                self._stop_instance(
                    int(row["topic_id"]),
                    int(row["instance"]),
                    row["target_message_id"],
                    "send_unknown",
                    at,
                )
        return len(rows)

    def due(self, *, now: datetime, limit: int = PANEL_DUE_BATCH) -> tuple[PanelRecord, ...]:
        rows = self._connection.execute(
            """SELECT topic_id,chat_id,thread_id,instance,message_id,confirmed_digest,
                      refresh_requested_at
               FROM topic_panels p
               WHERE enabled=1 AND unavailable_reason IS NULL AND next_due_at<=?
                 AND NOT EXISTS (SELECT 1 FROM topic_panel_operations o
                                 WHERE o.topic_id=p.topic_id AND o.state='sending')
               ORDER BY next_due_at,topic_id LIMIT ?""",
            (panel_time(now), max(1, min(limit, PANEL_DUE_BATCH))),
        ).fetchall()
        return tuple(
            PanelRecord(
                int(row["topic_id"]),
                int(row["chat_id"]),
                int(row["thread_id"]),
                int(row["instance"]),
                None if row["message_id"] is None else int(row["message_id"]),
                row["confirmed_digest"],
                row["refresh_requested_at"],
            )
            for row in rows
        )

    def defer_unchanged(self, panel: PanelRecord, *, next_due_at: datetime, now: datetime) -> bool:
        """Record a check without HTTP, only while the same shown version is current."""
        if panel.message_id is None or panel.confirmed_digest is None:
            raise ValueError("only a confirmed panel can be unchanged")
        at = panel_time(now)
        with self._state._immediate_transaction():
            cursor = self._connection.execute(
                f"""UPDATE topic_panels SET {_SERVED_REFRESH},updated_at=?
                    WHERE topic_id=? AND instance=? AND enabled=1
                      AND unavailable_reason IS NULL AND message_id=? AND confirmed_digest=?""",
                (
                    panel.refresh_requested_at,
                    panel_time(next_due_at),
                    panel.refresh_requested_at,
                    at,
                    panel.topic_id,
                    panel.instance,
                    panel.message_id,
                    panel.confirmed_digest,
                ),
            )
        return cursor.rowcount == 1

    def postpone(self, panel: PanelRecord, *, next_due_at: datetime, now: datetime) -> None:
        """Back off after a local snapshot failure; a newer press stays due."""
        with self._state._immediate_transaction():
            self._connection.execute(
                """UPDATE topic_panels SET next_due_at=?,updated_at=?
                   WHERE topic_id=? AND instance=? AND refresh_requested_at IS ?""",
                (
                    panel_time(next_due_at),
                    panel_time(now),
                    panel.topic_id,
                    panel.instance,
                    panel.refresh_requested_at,
                ),
            )

    def stop(self, panel: PanelRecord, *, reason: str, now: datetime) -> None:
        """Stop this instance without HTTP, e.g. when its group is no longer registered."""
        with self._state._immediate_transaction():
            self._stop_instance(
                panel.topic_id, panel.instance, panel.message_id, reason, panel_time(now)
            )

    def paused(self, *, now: datetime) -> bool:
        """Whether a bot-wide Telegram rate-limit pause still covers panel requests."""
        row = self._connection.execute(
            "SELECT 1 FROM topic_panel_pause WHERE singleton=1 AND paused_until>?",
            (panel_time(now),),
        ).fetchone()
        return row is not None

    def begin(
        self,
        panel: PanelRecord,
        *,
        payload_html: str,
        reply_markup: dict[str, Any],
        body_digest: str,
        now: datetime,
    ) -> PanelOperation | None:
        """Commit one immutable send request before HTTP, or refuse a changed panel."""
        at = panel_time(now)
        operation = PanelOperation(
            operation_id=uuid.uuid4().hex,
            topic_id=panel.topic_id,
            instance=panel.instance,
            kind="create" if panel.message_id is None else "edit",
            chat_id=panel.chat_id,
            thread_id=panel.thread_id,
            target_message_id=panel.message_id,
            payload_html=payload_html,
            reply_markup=reply_markup,
            body_digest=body_digest,
            lease_token=uuid.uuid4().hex,
            served_refresh=panel.refresh_requested_at,
        )
        with self._state._immediate_transaction():
            # Recheck everything the caller observed: another sender may have
            # confirmed a newer version or recorded a rate-limit pause meanwhile.
            current = self._connection.execute(
                """SELECT 1 FROM topic_panels p
                   WHERE topic_id=? AND instance=? AND enabled=1
                     AND unavailable_reason IS NULL AND message_id IS ?
                     AND confirmed_digest IS ? AND next_due_at<=?
                     AND NOT EXISTS (SELECT 1 FROM topic_panel_operations o
                                     WHERE o.topic_id=p.topic_id AND o.state='sending')
                     AND NOT EXISTS (SELECT 1 FROM topic_panel_pause
                                     WHERE singleton=1 AND paused_until>?)""",
                (panel.topic_id, panel.instance, panel.message_id, panel.confirmed_digest, at, at),
            ).fetchone()
            if current is None:
                return None
            self._connection.execute(
                """INSERT INTO topic_panel_operations(operation_id,topic_id,instance,kind,
                       chat_id,thread_id,target_message_id,payload_html,reply_markup,
                       body_digest,state,lease_token,lease_expires_at,send_started_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'sending',?,?,?)""",
                (
                    operation.operation_id,
                    operation.topic_id,
                    operation.instance,
                    operation.kind,
                    operation.chat_id,
                    operation.thread_id,
                    operation.target_message_id,
                    payload_html,
                    json.dumps(reply_markup, ensure_ascii=False, sort_keys=True),
                    body_digest,
                    operation.lease_token,
                    panel_time(now + timedelta(seconds=PANEL_SEND_LEASE_SECONDS)),
                    at,
                ),
            )
        return operation

    def confirm(
        self,
        operation: PanelOperation,
        *,
        receipt_message_id: int,
        next_due_at: datetime,
        now: datetime,
    ) -> bool:
        """Record a positive receipt; only the same instance and target show it."""
        if receipt_message_id <= 0 or (
            operation.kind == "edit" and receipt_message_id != operation.target_message_id
        ):
            raise ValueError("panel receipt does not match its operation")
        at = panel_time(now)
        with self._state._immediate_transaction():
            if not self._settle(operation, "confirmed", receipt_message_id, None, at):
                return False
            self._connection.execute(
                f"""UPDATE topic_panels SET message_id=?,confirmed_digest=?,confirmed_at=?,
                        confirmed_operation_id=?,{_SERVED_REFRESH},updated_at=?
                    WHERE topic_id=? AND instance=? AND enabled=1
                      AND unavailable_reason IS NULL AND message_id IS ?""",
                (
                    receipt_message_id,
                    operation.body_digest,
                    at,
                    operation.operation_id,
                    operation.served_refresh,
                    panel_time(next_due_at),
                    operation.served_refresh,
                    at,
                    operation.topic_id,
                    operation.instance,
                    operation.target_message_id,
                ),
            )
        return True

    def reject(
        self,
        operation: PanelOperation,
        *,
        error_code: str,
        now: datetime,
        retry_at: datetime | None,
        pause_until: datetime | None = None,
    ) -> bool:
        """Record a proven non-effect; retry later or stop this instance.

        ``pause_until`` records a bot-wide rate-limit pause in the same
        transaction, so neither a restart nor a new press shortens it.
        """
        at = panel_time(now)
        with self._state._immediate_transaction():
            if not self._settle(operation, "rejected", None, error_code, at):
                return False
            if pause_until is not None:
                self._connection.execute(
                    """INSERT INTO topic_panel_pause(singleton,paused_until,updated_at)
                       VALUES(1,?,?)
                       ON CONFLICT(singleton) DO UPDATE SET
                           paused_until=MAX(paused_until,excluded.paused_until),
                           updated_at=excluded.updated_at""",
                    (panel_time(pause_until), at),
                )
            if retry_at is None:
                self._stop_instance(
                    operation.topic_id,
                    operation.instance,
                    operation.target_message_id,
                    error_code,
                    at,
                )
            else:
                self._connection.execute(
                    """UPDATE topic_panels SET next_due_at=?,updated_at=?
                       WHERE topic_id=? AND instance=? AND message_id IS ?""",
                    (
                        panel_time(retry_at),
                        at,
                        operation.topic_id,
                        operation.instance,
                        operation.target_message_id,
                    ),
                )
        return True

    def mark_unknown(self, operation: PanelOperation, *, error_code: str, now: datetime) -> bool:
        """The request may have applied: keep evidence and stop this instance."""
        at = panel_time(now)
        with self._state._immediate_transaction():
            if not self._settle(operation, "unknown", None, error_code, at):
                return False
            self._stop_instance(
                operation.topic_id,
                operation.instance,
                operation.target_message_id,
                "send_unknown",
                at,
            )
        return True

    def _settle(
        self,
        operation: PanelOperation,
        state: str,
        receipt_message_id: int | None,
        error_code: str | None,
        at: str,
    ) -> bool:
        cursor = self._connection.execute(
            """UPDATE topic_panel_operations
               SET state=?,receipt_message_id=?,error_code=?,completed_at=?
               WHERE operation_id=? AND lease_token=? AND state='sending'""",
            (
                state,
                receipt_message_id,
                None if error_code is None else error_code[:64],
                at,
                operation.operation_id,
                operation.lease_token,
            ),
        )
        if cursor.rowcount != 1:
            return False
        self._connection.execute(
            """DELETE FROM topic_panel_operations
               WHERE topic_id=? AND state IN ('confirmed','rejected')
                 AND operation_id NOT IN (
                     SELECT operation_id FROM topic_panel_operations
                     WHERE topic_id=? AND state IN ('confirmed','rejected')
                     ORDER BY send_started_at DESC,operation_id DESC LIMIT ?)
                 AND operation_id NOT IN (
                     SELECT confirmed_operation_id FROM topic_panels
                     WHERE topic_id=? AND confirmed_operation_id IS NOT NULL)
                 AND operation_id<>?""",
            (
                operation.topic_id,
                operation.topic_id,
                PANEL_SETTLED_RETENTION,
                operation.topic_id,
                # A wall-clock step back must not prune the operation being settled.
                operation.operation_id,
            ),
        )
        return True

    def _stop_instance(
        self, topic_id: int, instance: int, target_message_id: int | None, reason: str, at: str
    ) -> None:
        self._connection.execute(
            """UPDATE topic_panels SET unavailable_reason=?,refresh_requested_at=NULL,updated_at=?
               WHERE topic_id=? AND instance=? AND message_id IS ?""",
            (reason[:64], at, topic_id, instance, target_message_id),
        )
