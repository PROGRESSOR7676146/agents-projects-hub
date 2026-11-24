"""One agent mail delivery per external sender cycle (ADR 0112).

The sender reaches this step after final results, notices, progress and
session-connect markers had nothing to do, and before the optional topic
panel. A display posts the letter as a Hub-bot document with the owner-only
«Обработать» button in the target topic; a failure notice tells the source
topic once that its queued letter was not shown. Only a real receipt confirms
a delivery; an attempted send without one stays unknown and is not repeated.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .agent_letter_state import AgentLetterStore, LetterDelivery
from .agent_mail import mime_type, process_keyboard
from .diagnostic_log import survived
from .state import HubState
from .telegram import TelegramError, has_native_not_sent_proof

LETTER_RETRY_SECONDS = 30


class LetterTelegram(Protocol):
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

    def send_document_bytes(
        self,
        chat_id: int,
        thread_id: int,
        content: bytes,
        *,
        file_name: str,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]: ...


class AgentLetterSender:
    def __init__(
        self,
        state: HubState,
        api: LetterTelegram,
        *,
        transport_success: Callable[[], None],
        transport_failure: Callable[[TelegramError], None],
        record_event: Callable[[str, str, str], None],
        chat_registered: Callable[[int, str], bool],
    ) -> None:
        self.store = AgentLetterStore(state)
        self._api = api
        self._transport_success = transport_success
        self._transport_failure = transport_failure
        self._record_event = record_event
        self._chat_registered = chat_registered

    def deliver_one(self, *, now: datetime | None = None) -> bool:
        """Return True after one delivery attempt or settlement; never more than one."""
        current = now if now is not None else datetime.now(timezone.utc)
        recovered = self.store.recover_stale(now=current)
        if recovered:
            self._record_event("warning", "agent_letter_send_unknown", f"lease_expired={recovered}")
        delivery = self.store.claim_next(now=current)
        if delivery is None:
            return bool(recovered)
        if delivery.kind == "display" and not self.store.route_authorized(delivery):
            # Mail was disabled or this route changed before the letter was shown.
            self.store.settle(delivery, "rejected", "route_removed", now=current)
            self._record_event("warning", "agent_letter_rejected", "reason=route_removed")
            return True
        route = delivery.route
        if not (
            self.store.topic_bound(delivery)
            and self._chat_registered(delivery.chat_id, delivery.project_id)
            and (
                delivery.kind != "display"
                # A display discloses the source's letter: both groups must
                # still belong to their projects, not only the historical rows.
                or self._chat_registered(route.source_chat_id, route.source_project_id)
            )
        ):
            # The topic no longer belongs to the project the letter names for it.
            self.store.settle(delivery, "rejected", "topic_unbound", now=current)
            self._record_event("warning", "agent_letter_rejected", f"kind={delivery.kind}")
            return True
        try:
            if delivery.kind == "display":
                message = self._api.send_document_bytes(
                    delivery.chat_id,
                    delivery.thread_id,
                    delivery.body.encode("utf-8"),
                    file_name=delivery.file_name,
                    caption=delivery.caption_html,
                    reply_markup=process_keyboard(delivery.letter_id),
                    mime_type=mime_type(delivery.file_name),
                )
                document = message.get("document")
                document = document if isinstance(document, dict) else {}
                file_id = _text(document.get("file_id"))
                file_unique_id = _text(document.get("file_unique_id"))
                if file_id is None or file_unique_id is None:
                    # Shown, but not processable: never resend, tell the source once.
                    self.store.settle(
                        delivery,
                        "unknown",
                        "incomplete_receipt",
                        now=current,
                        receipt_message_id=int(message["message_id"]),
                    )
                    self._record_event("warning", "agent_letter_send_unknown", "incomplete_receipt")
                    return True
                self.store.confirm(
                    delivery,
                    message_id=int(message["message_id"]),
                    file_id=file_id,
                    file_unique_id=file_unique_id,
                    now=current,
                )
            else:
                message_id = self._api.send_html(
                    delivery.chat_id, delivery.thread_id, delivery.failure_html
                )
                self.store.confirm(
                    delivery, message_id=message_id, file_id=None, file_unique_id=None, now=current
                )
        except TelegramError as error:
            self._settle_error(delivery, error, now=current)
            return True
        except Exception as error:
            survived("agent_letter_sender.send", error)
            self.store.settle(delivery, "unknown", "unexpected_client", now=current)
            self._record_event("warning", "agent_letter_send_unknown", f"kind={delivery.kind}")
            return True
        self._transport_success()
        return True

    def _settle_error(
        self, delivery: LetterDelivery, error: TelegramError, *, now: datetime
    ) -> None:
        status = error.status_code
        rejected = error.failure_class == "api_rejection" and status is not None
        if rejected and status in (401, 429):
            delay = (
                error.retry_after if status == 429 and error.retry_after else LETTER_RETRY_SECONDS
            )
            self.store.retry(
                delivery, error_code=f"api_{status}", retry_at=now + timedelta(seconds=delay)
            )
            self._transport_failure(error)
            return
        if rejected and status is not None and 400 <= status < 500:
            # A deleted topic, lost rights or an invalid request: not retried.
            self.store.settle(delivery, "rejected", f"api_{status}", now=now)
            self._record_event(
                "warning", "agent_letter_rejected", f"kind={delivery.kind};status={status}"
            )
            return
        if error.failure_class == "local_validation":
            self.store.settle(delivery, "rejected", "local_validation", now=now)
            self._record_event("warning", "agent_letter_rejected", f"kind={delivery.kind}")
            return
        if has_native_not_sent_proof(error):
            self.store.retry(
                delivery,
                error_code=error.failure_class[:64],
                retry_at=now + timedelta(seconds=LETTER_RETRY_SECONDS),
            )
            self._transport_failure(error)
            return
        self.store.settle(delivery, "unknown", error.failure_class[:64], now=now)
        self._transport_failure(error)
        self._record_event(
            "warning",
            "agent_letter_send_unknown",
            f"kind={delivery.kind};class={error.failure_class}",
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and 1 <= len(value) <= 512 else None
