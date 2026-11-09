"""One Hub-Bot-only, owner-bound release of an unknown delivery FIFO hold.

This module deliberately does not send Telegram traffic.  The Controller owns
the real Hub-Bot update path and calls these methods around a normal inline
keyboard send.  A token becomes usable only after the positive message ID is
durably bound; a send/persistence gap therefore fails closed.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from . import artifact_capabilities
from .delivery_hold_predicates import outbox_delivery_hold_released
from .delivery_hold_state import ACTION, DeliveryHoldState

CALLBACK_PREFIX = "dho:a:"
COPY_CALLBACK_PREFIX = "dho:c:"
CALLBACK_ORIGIN = "hub"
REMOTE_AUTHORITY = "telegram_owner_callback"
TOKEN_BYTES = 18
TOKEN_TTL = timedelta(minutes=10)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{24}")
_BINDING_FIELDS = (
    "outbox_id",
    "job_id",
    "result_id",
    "topic_id",
    "topic_sequence",
    "project_id",
    "execution_scope",
    "session_id",
    "session_generation",
    "sender_agent_id",
    "chat_id",
    "thread_id",
)


@dataclass(frozen=True, slots=True)
class TelegramDeliveryHoldPreview:
    """A transient capability until the Hub Bot message is positively returned."""

    token_id: str
    token: str
    snapshot: str
    outbox_id: str
    job_id: str
    result_id: str
    sender_agent_id: str
    chat_id: int
    thread_id: int
    owner_user_id: int
    hub_bot_user_id: int
    notice_id: str
    notice_message_id: int
    part_count: int
    receipted_parts: int
    issued_at: str
    expires_at: str

    @property
    def callback_data(self) -> str:
        return CALLBACK_PREFIX + self.token

    @property
    def copy_callback_data(self) -> str:
        return COPY_CALLBACK_PREFIX + self.token


@dataclass(frozen=True, slots=True)
class TelegramDeliveryHoldDisposition:
    outbox_id: str
    job_id: str
    authority: str
    applied_at: str
    hold_status: str


def preview_text(preview: TelegramDeliveryHoldPreview) -> str:
    """Safe visible explanation: never reveal the capability or snapshot digest."""
    return (
        "The earlier result is saved, but Telegram delivery is still unconfirmed. "
        f"Saved parts: {preview.part_count}; positively receipted parts: "
        f"{preview.receipted_parts}.\n\n"
        "Continuing permits only later queued work in this topic. Hub will not resend "
        "the uncertain result or repeat provider work. Session, writer, stop, approval, "
        "native-uncertainty, and other safety restrictions remain in force.\n\n"
        "Получите копию ответа в личном чате или отдельно разрешите продолжение очереди."
    )


class TelegramDeliveryHoldState:
    """Snapshot/CAS state only; HubState owns every transaction boundary."""

    def __init__(self, db: sqlite3.Connection, error: Callable[[str], Exception]) -> None:
        self.db = db
        self.error = error

    def _require_transaction(self) -> None:
        if not self.db.in_transaction:
            raise self.error("Telegram delivery-hold recovery requires a state transaction")

    def _require_enabled_artifact(self) -> None:
        if not artifact_capabilities.REMOTE_DELIVERY_HOLD_RECOVERY:
            raise self.error(
                "Remote delivery recovery is unavailable in this build; use local delivery-hold"
            )

    def _timestamp(self, now: datetime) -> tuple[datetime, str]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise self.error("Telegram delivery-hold time must be timezone-aware")
        current = now.astimezone(timezone.utc)
        return current, current.isoformat()

    def _stored_time(self, value: object, label: str) -> datetime:
        if not isinstance(value, str):
            raise self.error(f"invalid Telegram delivery-hold {label}")
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise self.error(f"invalid Telegram delivery-hold {label}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise self.error(f"invalid Telegram delivery-hold {label}")
        return parsed.astimezone(timezone.utc)

    def _owner(self, owner_user_id: int, owner_user_ids: Sequence[int]) -> None:
        if (
            not isinstance(owner_user_id, int)
            or isinstance(owner_user_id, bool)
            or owner_user_id <= 0
            or owner_user_id not in owner_user_ids
        ):
            raise self.error("Telegram delivery-hold recovery requires a configured owner")

    def _integer(self, value: int, name: str, *, positive: bool = True) -> None:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or (value <= 0 if positive else value == 0)
        ):
            raise self.error(f"invalid Telegram delivery-hold {name}")

    @staticmethod
    def _token_sha256(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _validate_token(self, token: str) -> None:
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise self.error("Telegram delivery-hold preview is unavailable")

    def _warning(
        self, *, chat_id: int, thread_id: int, notice_message_id: int
    ) -> tuple[dict[str, Any], str]:
        self._integer(chat_id, "chat", positive=False)
        self._integer(thread_id, "topic")
        self._integer(notice_message_id, "warning message")
        rows = self.db.execute(
            """SELECT n.notice_id,n.event_key,n.kind,n.job_id,n.chat_id,n.thread_id,
                      n.reply_to_message_id,n.telegram_message_id,n.status,n.attempt_count,
                      n.send_started_at,o.outbox_id
                 FROM task_lifecycle_notices n
                 JOIN telegram_outbox o ON n.event_key='delivery-wait:' || o.outbox_id
                 JOIN provider_jobs j ON j.job_id=o.job_id
                 JOIN topics t ON t.topic_id=j.topic_id
                 WHERE n.kind='delivery_wait' AND n.status='delivered'
                   AND n.telegram_message_id=? AND n.chat_id=? AND n.thread_id=?
                   AND n.job_id=o.job_id AND o.chat_id=n.chat_id AND o.thread_id=n.thread_id
                   AND j.chat_id=n.chat_id AND t.chat_id=n.chat_id AND t.thread_id=n.thread_id
                 LIMIT 2""",
            (notice_message_id, chat_id, thread_id),
        ).fetchall()
        if len(rows) != 1:
            raise self.error("Reply to the current Hub delivery warning in this topic")
        row = dict(rows[0])
        return row, str(row["outbox_id"])

    def _target(self, outbox_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        binding, target = DeliveryHoldState(self.db, self.error)._target(outbox_id)
        if target["job"]["status"] != "result_ready" or binding["result_id"] is None:
            raise self.error("Telegram recovery is available only for a saved final result")
        return binding, target

    @staticmethod
    def _warning_snapshot(warning: dict[str, Any]) -> dict[str, Any]:
        return {
            key: warning[key]
            for key in (
                "notice_id",
                "event_key",
                "kind",
                "job_id",
                "chat_id",
                "thread_id",
                "reply_to_message_id",
                "telegram_message_id",
                "status",
                "attempt_count",
                "send_started_at",
            )
        }

    def _snapshot(
        self,
        binding: dict[str, Any],
        target: dict[str, Any],
        warning: dict[str, Any],
        *,
        owner_user_id: int,
        hub_bot_user_id: int,
    ) -> str:
        return DeliveryHoldState._digest(
            {
                "action": ACTION,
                "version": 1,
                "authority": REMOTE_AUTHORITY,
                "callback_origin": CALLBACK_ORIGIN,
                "owner_user_id": owner_user_id,
                "hub_bot_user_id": hub_bot_user_id,
                "binding": binding,
                "target": target,
                "delivery_wait_warning": self._warning_snapshot(warning),
            }
        )

    def _assert_no_existing_disposition(self, outbox_id: str) -> None:
        if (
            self.db.execute(
                "SELECT 1 FROM telegram_delivery_hold_dispositions WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            is not None
            or self.db.execute(
                "SELECT 1 FROM telegram_delivery_hold_remote_dispositions WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            is not None
        ):
            raise self.error("This delivery hold already has an immutable owner decision")

    def _assert_no_live_preview(self, outbox_id: str, current: datetime) -> None:
        """Retain expired audit rows but never mint a second usable preview."""
        rows = self.db.execute(
            """SELECT token.expires_at FROM telegram_delivery_hold_callback_tokens token
               LEFT JOIN telegram_delivery_hold_remote_dispositions disposition
                 ON disposition.token_id=token.token_id
               WHERE token.outbox_id=? AND disposition.token_id IS NULL""",
            (outbox_id,),
        ).fetchall()
        for row in rows:
            if self._stored_time(row["expires_at"], "preview expiry") > current:
                raise self.error("A Telegram delivery-hold preview is already active")

    def prepare_preview(
        self,
        *,
        owner_user_id: int,
        owner_user_ids: Sequence[int],
        hub_bot_user_id: int,
        chat_id: int,
        thread_id: int,
        notice_message_id: int,
        now: datetime,
    ) -> TelegramDeliveryHoldPreview:
        self._require_transaction()
        self._require_enabled_artifact()
        self._owner(owner_user_id, owner_user_ids)
        self._integer(hub_bot_user_id, "Hub Bot")
        current, issued_at = self._timestamp(now)
        warning, outbox_id = self._warning(
            chat_id=chat_id, thread_id=thread_id, notice_message_id=notice_message_id
        )
        self._assert_no_existing_disposition(outbox_id)
        self._assert_no_live_preview(outbox_id, current)
        binding, target = self._target(outbox_id)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        if _TOKEN.fullmatch(token) is None:
            raise self.error("could not create a bounded Telegram delivery-hold token")
        return TelegramDeliveryHoldPreview(
            token_id=uuid.uuid4().hex,
            token=token,
            snapshot=self._snapshot(
                binding,
                target,
                warning,
                owner_user_id=owner_user_id,
                hub_bot_user_id=hub_bot_user_id,
            ),
            outbox_id=outbox_id,
            job_id=str(binding["job_id"]),
            result_id=str(binding["result_id"]),
            sender_agent_id=str(binding["sender_agent_id"]),
            chat_id=chat_id,
            thread_id=thread_id,
            owner_user_id=owner_user_id,
            hub_bot_user_id=hub_bot_user_id,
            notice_id=str(warning["notice_id"]),
            notice_message_id=notice_message_id,
            part_count=len(target["parts"]),
            receipted_parts=sum(
                part["telegram_message_id"] is not None for part in target["parts"]
            ),
            issued_at=issued_at,
            expires_at=(current + TOKEN_TTL).isoformat(),
        )

    def bind_preview_message(
        self,
        preview: TelegramDeliveryHoldPreview,
        *,
        owner_user_ids: Sequence[int],
        preview_message_id: int,
        now: datetime,
    ) -> None:
        """Persist only after Telegram returned the exact preview message ID."""
        self._require_transaction()
        self._require_enabled_artifact()
        if not isinstance(preview, TelegramDeliveryHoldPreview):
            raise self.error("invalid Telegram delivery-hold preview")
        self._owner(preview.owner_user_id, owner_user_ids)
        self._validate_token(preview.token)
        self._integer(preview.hub_bot_user_id, "Hub Bot")
        self._integer(preview_message_id, "preview message")
        current, _ = self._timestamp(now)
        issued_at = self._stored_time(preview.issued_at, "preview issue time")
        expires_at = self._stored_time(preview.expires_at, "preview expiry")
        if issued_at > current or expires_at - issued_at != TOKEN_TTL:
            raise self.error("invalid Telegram delivery-hold preview expiry")
        if expires_at <= current:
            raise self.error("Telegram delivery-hold preview expired; reply /delivery again")
        warning, outbox_id = self._warning(
            chat_id=preview.chat_id,
            thread_id=preview.thread_id,
            notice_message_id=preview.notice_message_id,
        )
        if str(warning["notice_id"]) != preview.notice_id or outbox_id != preview.outbox_id:
            raise self.error("Telegram delivery-hold warning changed; reply /delivery again")
        binding, target = self._target(preview.outbox_id)
        if (
            str(binding["job_id"]) != preview.job_id
            or str(binding["sender_agent_id"]) != preview.sender_agent_id
            or self._snapshot(
                binding,
                target,
                warning,
                owner_user_id=preview.owner_user_id,
                hub_bot_user_id=preview.hub_bot_user_id,
            )
            != preview.snapshot
        ):
            raise self.error("Telegram delivery-hold preview changed; reply /delivery again")
        self._assert_no_existing_disposition(preview.outbox_id)
        self._assert_no_live_preview(preview.outbox_id, current)
        values = {
            "token_id": preview.token_id,
            "token_sha256": self._token_sha256(preview.token),
            **binding,
            "owner_user_id": preview.owner_user_id,
            "hub_bot_user_id": preview.hub_bot_user_id,
            "notice_id": preview.notice_id,
            "notice_message_id": preview.notice_message_id,
            "preview_message_id": preview_message_id,
            "callback_origin": CALLBACK_ORIGIN,
            "authority": REMOTE_AUTHORITY,
            "action": ACTION,
            "snapshot_version": 1,
            "snapshot": preview.snapshot,
            "issued_at": preview.issued_at,
            "expires_at": preview.expires_at,
        }
        self.db.execute(
            "INSERT INTO telegram_delivery_hold_callback_tokens "
            f"({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def _token_callback_binding(
        self,
        row: sqlite3.Row,
        *,
        owner_user_id: int,
        chat_id: int,
        thread_id: int,
        callback_message_id: int,
        hub_bot_user_id: int,
        callback_origin: str,
    ) -> None:
        if (
            row["owner_user_id"] != owner_user_id
            or row["chat_id"] != chat_id
            or row["thread_id"] != thread_id
            or row["preview_message_id"] != callback_message_id
            or row["hub_bot_user_id"] != hub_bot_user_id
            or row["callback_origin"] != callback_origin
            or row["authority"] != REMOTE_AUTHORITY
            or row["action"] != ACTION
            or row["snapshot_version"] != 1
        ):
            raise self.error("Telegram delivery-hold preview is not valid for this callback")

    def _token_matches_current(
        self, row: sqlite3.Row, binding: dict[str, Any], snapshot: str
    ) -> None:
        if (
            any(row[field] != binding[field] for field in _BINDING_FIELDS)
            or row["snapshot"] != snapshot
        ):
            raise self.error("Telegram delivery-hold preview is stale; reply /delivery again")

    def _record(self, row: sqlite3.Row) -> TelegramDeliveryHoldDisposition:
        effect = self.db.execute(
            f"SELECT {outbox_delivery_hold_released('o')} FROM telegram_outbox o "
            "WHERE o.outbox_id=?",
            (row["outbox_id"],),
        ).fetchone()
        return TelegramDeliveryHoldDisposition(
            outbox_id=str(row["outbox_id"]),
            job_id=str(row["job_id"]),
            authority=str(row["authority"]),
            applied_at=str(row["applied_at"]),
            hold_status=(
                "released_by_telegram_owner"
                if effect is not None and effect[0]
                else "binding_changed"
            ),
        )

    def read_private_copy(
        self,
        token: str,
        *,
        owner_user_id: int,
        owner_user_ids: Sequence[int],
        chat_id: int,
        thread_id: int,
        callback_message_id: int,
        hub_bot_user_id: int,
        callback_origin: str,
        now: datetime,
    ) -> tuple[str, str, bool]:
        """Validate the issued snapshot without recording a decision or changing delivery."""
        self._require_transaction()
        self._require_enabled_artifact()
        self._owner(owner_user_id, owner_user_ids)
        self._validate_token(token)
        self._integer(chat_id, "chat", positive=False)
        self._integer(thread_id, "topic")
        self._integer(callback_message_id, "preview message")
        self._integer(hub_bot_user_id, "Hub Bot")
        if callback_origin != CALLBACK_ORIGIN:
            raise self.error("invalid Telegram delivery-hold callback")
        current, _ = self._timestamp(now)
        row = self.db.execute(
            "SELECT * FROM telegram_delivery_hold_callback_tokens WHERE token_sha256=?",
            (self._token_sha256(token),),
        ).fetchone()
        if row is None:
            raise self.error("Telegram delivery-hold preview is unavailable")
        self._token_callback_binding(
            row,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            callback_message_id=callback_message_id,
            hub_bot_user_id=hub_bot_user_id,
            callback_origin=callback_origin,
        )
        issued_at = self._stored_time(row["issued_at"], "preview issue time")
        expires_at = self._stored_time(row["expires_at"], "preview expiry")
        if issued_at > current or expires_at - issued_at != TOKEN_TTL or expires_at <= current:
            raise self.error("Telegram delivery-hold preview expired")
        self._assert_no_existing_disposition(str(row["outbox_id"]))
        warning, outbox_id = self._warning(
            chat_id=chat_id, thread_id=thread_id, notice_message_id=int(row["notice_message_id"])
        )
        if str(warning["notice_id"]) != row["notice_id"] or outbox_id != row["outbox_id"]:
            raise self.error("Telegram delivery-hold warning changed")
        binding, target = self._target(outbox_id)
        self._token_matches_current(
            row,
            binding,
            self._snapshot(
                binding,
                target,
                warning,
                owner_user_id=owner_user_id,
                hub_bot_user_id=hub_bot_user_id,
            ),
        )
        topic = self.db.execute(
            "SELECT title FROM topics WHERE topic_id=?", (row["topic_id"],)
        ).fetchone()
        if topic is None:
            raise self.error("Telegram delivery-hold topic unavailable")
        return (
            str(target["result"]["visible_response"]),
            str(topic["title"]),
            any(part["part_type"] != "text" for part in target["parts"]),
        )

    def apply_callback(
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
        now: datetime,
    ) -> TelegramDeliveryHoldDisposition:
        self._require_transaction()
        self._require_enabled_artifact()
        self._owner(owner_user_id, owner_user_ids)
        self._validate_token(token)
        self._integer(chat_id, "chat", positive=False)
        self._integer(thread_id, "topic")
        self._integer(callback_message_id, "preview message")
        self._integer(hub_bot_user_id, "Hub Bot")
        if (
            not isinstance(callback_id, str)
            or not 1 <= len(callback_id) <= 200
            or not callback_id.isascii()
            or callback_origin != CALLBACK_ORIGIN
        ):
            raise self.error("invalid Telegram delivery-hold callback")
        current, applied_at = self._timestamp(now)
        row = self.db.execute(
            "SELECT * FROM telegram_delivery_hold_callback_tokens WHERE token_sha256=?",
            (self._token_sha256(token),),
        ).fetchone()
        if row is None:
            raise self.error("Telegram delivery-hold preview is unavailable")
        self._token_callback_binding(
            row,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            callback_message_id=callback_message_id,
            hub_bot_user_id=hub_bot_user_id,
            callback_origin=callback_origin,
        )
        existing = self.db.execute(
            "SELECT * FROM telegram_delivery_hold_remote_dispositions WHERE token_id=?",
            (row["token_id"],),
        ).fetchone()
        if existing is not None:
            return self._record(existing)
        issued_at = self._stored_time(row["issued_at"], "preview issue time")
        expires_at = self._stored_time(row["expires_at"], "preview expiry")
        if issued_at > current or expires_at - issued_at != TOKEN_TTL:
            raise self.error("invalid Telegram delivery-hold preview expiry")
        if expires_at <= current:
            raise self.error("Telegram delivery-hold preview expired; reply /delivery again")
        if (
            self.db.execute(
                "SELECT 1 FROM telegram_delivery_hold_remote_dispositions WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            is not None
        ):
            raise self.error("Telegram delivery-hold callback was already used")
        warning, outbox_id = self._warning(
            chat_id=chat_id,
            thread_id=thread_id,
            notice_message_id=int(row["notice_message_id"]),
        )
        if str(warning["notice_id"]) != row["notice_id"] or outbox_id != row["outbox_id"]:
            raise self.error("Telegram delivery-hold warning changed; reply /delivery again")
        binding, target = self._target(str(row["outbox_id"]))
        self._token_matches_current(
            row,
            binding,
            self._snapshot(
                binding,
                target,
                warning,
                owner_user_id=owner_user_id,
                hub_bot_user_id=hub_bot_user_id,
            ),
        )
        if (
            self.db.execute(
                "SELECT 1 FROM telegram_delivery_hold_dispositions WHERE outbox_id=?",
                (row["outbox_id"],),
            ).fetchone()
            is not None
        ):
            raise self.error("This delivery hold already has a local immutable decision")
        other = self.db.execute(
            "SELECT 1 FROM telegram_delivery_hold_remote_dispositions WHERE outbox_id=?",
            (row["outbox_id"],),
        ).fetchone()
        if other is not None:
            raise self.error("This delivery hold already has a remote immutable decision")
        values = {
            "disposition_id": uuid.uuid4().hex,
            "token_id": row["token_id"],
            "callback_id": callback_id,
            **{field: row[field] for field in _BINDING_FIELDS},
            "owner_user_id": owner_user_id,
            "hub_bot_user_id": hub_bot_user_id,
            "notice_id": row["notice_id"],
            "notice_message_id": row["notice_message_id"],
            "callback_message_id": callback_message_id,
            "callback_origin": CALLBACK_ORIGIN,
            "authority": REMOTE_AUTHORITY,
            "action": ACTION,
            "snapshot_version": 1,
            "snapshot": row["snapshot"],
            "applied_at": applied_at,
        }
        self.db.execute(
            "INSERT INTO telegram_delivery_hold_remote_dispositions "
            f"({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
        created = self.db.execute(
            "SELECT * FROM telegram_delivery_hold_remote_dispositions WHERE token_id=?",
            (row["token_id"],),
        ).fetchone()
        assert created is not None
        return self._record(created)
