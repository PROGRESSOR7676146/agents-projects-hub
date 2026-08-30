from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TopicMessage:
    update_id: int
    message_id: int
    chat_id: int
    thread_id: int
    chat_title: str
    sender_id: int
    text: str
    reply_to_username: str | None = None


@dataclass(frozen=True, slots=True)
class TopicCallback:
    callback_id: str
    message_id: int
    chat_id: int
    thread_id: int
    sender_id: int
    data: str


def parse_topic_message(update: dict[str, Any]) -> TopicMessage | None:
    message = update.get("message")
    if not isinstance(message, dict) or message.get("from", {}).get("is_bot"):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    text = message.get("text")
    raw_thread_id = message.get("message_thread_id")
    reply_to_username = None
    reply = message.get("reply_to_message")
    # A manually selected Telegram quote is commentary for the active agent,
    # while a plain Reply is direct addressing of the original bot author.
    if isinstance(reply, dict) and not isinstance(message.get("quote"), dict):
        reply_author = reply.get("from")
        if (
            isinstance(reply_author, dict)
            and reply_author.get("is_bot") is True
            and isinstance(reply_author.get("username"), str)
        ):
            reply_to_username = str(reply_author["username"])
    # Telegram omits message_thread_id for the General forum topic. Keep a
    # stable local numeric identity without pretending it is an API thread id.
    thread_id = raw_thread_id if isinstance(raw_thread_id, int) else 1
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "supergroup"
        or (not message.get("is_topic_message") and not chat.get("is_forum"))
        or not isinstance(sender, dict)
        or not isinstance(sender.get("id"), int)
        or not isinstance(text, str)
    ):
        return None
    return TopicMessage(
        update_id=int(update["update_id"]),
        message_id=int(message["message_id"]),
        chat_id=int(chat["id"]),
        thread_id=thread_id,
        chat_title=str(chat.get("title") or chat["id"]),
        sender_id=int(sender["id"]),
        text=text,
        reply_to_username=reply_to_username,
    )


def parse_direct_message(update: dict[str, Any]) -> TopicMessage | None:
    """Parse an owner-to-bot private message without treating groups as DMs."""
    message = update.get("message")
    if not isinstance(message, dict) or message.get("from", {}).get("is_bot"):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    text = message.get("text")
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "private"
        or not isinstance(chat.get("id"), int)
        or not isinstance(sender, dict)
        or not isinstance(sender.get("id"), int)
        or chat["id"] != sender["id"]
        or not isinstance(text, str)
    ):
        return None
    raw_thread_id = message.get("message_thread_id")
    return TopicMessage(
        update_id=int(update["update_id"]),
        message_id=int(message["message_id"]),
        chat_id=int(chat["id"]),
        thread_id=raw_thread_id if isinstance(raw_thread_id, int) else 1,
        chat_title="Direct",
        sender_id=int(sender["id"]),
        text=text,
    )


def parse_topic_callback(update: dict[str, Any]) -> TopicCallback | None:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    message = callback.get("message")
    sender = callback.get("from")
    data = callback.get("data")
    callback_id = callback.get("id")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return None
    chat = message.get("chat")
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "supergroup"
        or not isinstance(sender.get("id"), int)
        or not isinstance(data, str)
        or not isinstance(callback_id, str)
    ):
        return None
    raw_thread_id = message.get("message_thread_id")
    thread_id = raw_thread_id if isinstance(raw_thread_id, int) else 1
    return TopicCallback(
        callback_id=callback_id,
        message_id=int(message["message_id"]),
        chat_id=int(chat["id"]),
        thread_id=thread_id,
        sender_id=int(sender["id"]),
        data=data,
    )


def parse_direct_callback(update: dict[str, Any]) -> TopicCallback | None:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    message = callback.get("message")
    sender = callback.get("from")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return None
    chat = message.get("chat")
    data = callback.get("data")
    callback_id = callback.get("id")
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "private"
        or not isinstance(chat.get("id"), int)
        or not isinstance(sender.get("id"), int)
        or chat["id"] != sender["id"]
        or not isinstance(data, str)
        or not isinstance(callback_id, str)
    ):
        return None
    raw_thread_id = message.get("message_thread_id")
    return TopicCallback(
        callback_id=callback_id,
        message_id=int(message["message_id"]),
        chat_id=int(chat["id"]),
        thread_id=raw_thread_id if isinstance(raw_thread_id, int) else 1,
        sender_id=int(sender["id"]),
        data=data,
    )


class TelegramBotApi:
    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token.strip() or ":" not in token or "\n" in token:
            raise TelegramError("invalid bot token")
        self._base = f"https://api.telegram.org/bot{token.strip()}/"
        self._opener = opener

    def call(self, method: str, **params: Any) -> Any:
        return self._call_with_timeout(method, request_timeout=8, **params)

    def _call_with_timeout(self, method: str, *, request_timeout: float, **params: Any) -> Any:
        body = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(self._base + method, data=body, method="POST")
        try:
            with self._opener(request, timeout=request_timeout) as response:
                document = json.load(response)
        except Exception as exc:
            raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from exc
        if not isinstance(document, dict) or not document.get("ok"):
            raise TelegramError("Telegram API rejected the request")
        return document.get("result")

    def updates(self, *, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset
        # Keep transport cancellation bounded beyond Telegram's server-side
        # long poll so service stop fits comfortably within the unit timeout.
        result = self._call_with_timeout("getUpdates", request_timeout=timeout + 5, **params)
        if not isinstance(result, list):
            raise TelegramError("getUpdates returned a non-list")
        return [item for item in result if isinstance(item, dict)]

    def send_html(
        self,
        chat_id: int,
        thread_id: int,
        html: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if thread_id != 1:
            params["message_thread_id"] = thread_id
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        result = self.call("sendMessage", **params)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("sendMessage returned an invalid result")
        return result["message_id"]

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        params: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        self.call("answerCallbackQuery", **params)
