from __future__ import annotations

import json
import mimetypes
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class TelegramError(RuntimeError):
    _OPERATIONS = frozenset(
        {
            "unknown",
            "api_call",
            "poll",
            "send_message",
            "send_document",
            "chat_action",
            "message_draft",
            "answer_callback",
        }
    )
    _FAILURE_CLASSES = frozenset(
        {
            "unknown",
            "api_http",
            "api_rejection",
            "network_timeout",
            "network_dns",
            "network_tls",
            "network_io",
            "invalid_response",
            "unexpected_transport",
            "local_validation",
            "local_io",
            "unexpected_client",
        }
    )

    def __init__(
        self,
        message: str,
        *,
        operation: str = "unknown",
        failure_class: str = "unknown",
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation if operation in self._OPERATIONS else "unknown"
        self.failure_class = failure_class if failure_class in self._FAILURE_CLASSES else "unknown"
        self.status_code = (
            status_code
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else None
        )
        self.retry_after = (
            retry_after
            if isinstance(retry_after, int)
            and not isinstance(retry_after, bool)
            and 0 <= retry_after <= 86_400
            else None
        )

    @property
    def signature(self) -> tuple[str, str, int | None]:
        # retry_after is intentionally excluded: a changing server hint must
        # not turn one outage into an event stream.
        return self.operation, self.failure_class, self.status_code

    @property
    def health_code(self) -> str:
        return f"telegram_{self.operation}_{self.failure_class}"[:128]

    def safe_detail(self, *, consecutive_failures: int, last_success: str | None) -> str:
        safe_last_success = (
            last_success
            if last_success is not None
            and len(last_success) <= 64
            and re.fullmatch(r"[0-9T:.+\-Z]+", last_success) is not None
            else "none"
        )
        fields = [
            f"operation={self.operation}",
            f"class={self.failure_class}",
            f"consecutive_failures={max(1, consecutive_failures)}",
            f"last_success={safe_last_success}",
        ]
        if self.status_code is not None:
            fields.append(f"status={self.status_code}")
        if self.retry_after is not None:
            fields.append(f"retry_after={self.retry_after}")
        return ";".join(fields)


def _operation(method: str) -> str:
    return {
        "getUpdates": "poll",
        "sendMessage": "send_message",
        "sendDocument": "send_document",
        "sendChatAction": "chat_action",
        "sendMessageDraft": "message_draft",
        "answerCallbackQuery": "answer_callback",
    }.get(method, "api_call")


def _retry_after(document: object) -> int | None:
    if not isinstance(document, dict):
        return None
    parameters = document.get("parameters")
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 86_400
        else None
    )


def _transport_error(method: str, exc: Exception) -> TelegramError:
    operation = _operation(method)
    status_code: int | None = None
    retry_after: int | None = None
    if isinstance(exc, urllib.error.HTTPError):
        status_code = exc.code if 100 <= exc.code <= 599 else None
        try:
            document = json.loads(exc.read(8192))
        except Exception:
            document = None
        retry_after = _retry_after(document)
        failure_class = "api_http"
    elif isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            failure_class = "network_timeout"
        elif isinstance(reason, socket.gaierror):
            failure_class = "network_dns"
        elif isinstance(reason, ssl.SSLError):
            failure_class = "network_tls"
        else:
            failure_class = "network_io"
    elif isinstance(exc, (TimeoutError, socket.timeout)):
        failure_class = "network_timeout"
    elif isinstance(exc, ssl.SSLError):
        failure_class = "network_tls"
    elif isinstance(exc, OSError):
        failure_class = "network_io"
    elif isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        failure_class = "invalid_response"
    else:
        failure_class = "unexpected_transport"
    return TelegramError(
        "Telegram transport request failed",
        operation=operation,
        failure_class=failure_class,
        status_code=status_code,
        retry_after=retry_after,
    )


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
    is_forwarded: bool = False


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
    is_forwarded = isinstance(message.get("forward_origin"), dict) or any(
        key in message
        for key in (
            "forward_from",
            "forward_from_chat",
            "forward_sender_name",
            "forward_date",
        )
    )
    reply = message.get("reply_to_message")
    # A manually selected Telegram quote is commentary for the active agent,
    # while a plain Reply is direct addressing of the original bot author.
    if (
        isinstance(reply, dict)
        and not isinstance(message.get("quote"), dict)
        and reply.get("message_id") != raw_thread_id
    ):
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
        is_forwarded=is_forwarded,
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
        is_forwarded=isinstance(message.get("forward_origin"), dict)
        or any(
            key in message
            for key in (
                "forward_from",
                "forward_from_chat",
                "forward_sender_name",
                "forward_date",
            )
        ),
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
            # The original urllib exception may contain the token-bearing URL.
            # Preserve only the explicitly bounded classification above.
            raise _transport_error(method, exc) from None
        if not isinstance(document, dict) or not document.get("ok"):
            status = document.get("error_code") if isinstance(document, dict) else None
            raise TelegramError(
                "Telegram API rejected the request",
                operation=_operation(method),
                failure_class="api_rejection" if isinstance(document, dict) else "invalid_response",
                status_code=status if isinstance(status, int) and 100 <= status <= 599 else None,
                retry_after=_retry_after(document),
            )
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
            raise TelegramError(
                "getUpdates returned a non-list",
                operation="poll",
                failure_class="invalid_response",
            )
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
            raise TelegramError(
                "sendMessage returned an invalid result",
                operation="send_message",
                failure_class="invalid_response",
            )
        return result["message_id"]

    def send_chat_action(self, chat_id: int, thread_id: int, action: str = "typing") -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if thread_id != 1:
            params["message_thread_id"] = thread_id
        if self._call_with_timeout("sendChatAction", request_timeout=2, **params) is not True:
            raise TelegramError(
                "sendChatAction returned an invalid result",
                operation="chat_action",
                failure_class="invalid_response",
            )

    def send_message_draft(
        self, chat_id: int, thread_id: int, *, draft_id: int, text: str = ""
    ) -> None:
        if chat_id <= 0:
            raise TelegramError(
                "message drafts require a private chat",
                operation="message_draft",
                failure_class="local_validation",
            )
        if draft_id == 0:
            raise TelegramError(
                "message draft id must be non-zero",
                operation="message_draft",
                failure_class="local_validation",
            )
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": text[:4096],
        }
        if thread_id != 1:
            params["message_thread_id"] = thread_id
        if self._call_with_timeout("sendMessageDraft", request_timeout=2, **params) is not True:
            raise TelegramError(
                "sendMessageDraft returned an invalid result",
                operation="message_draft",
                failure_class="invalid_response",
            )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        params: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        self.call("answerCallbackQuery", **params)

    def _call_multipart(
        self,
        method: str,
        *,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        request_timeout: float = 60.0,
    ) -> Any:
        boundary = uuid.uuid4().hex
        crlf = b"\r\n"
        body = bytearray()
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(value.encode("utf-8"))
            body.extend(crlf)
        for field_name, (filename, content, content_type) in files.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode(
                    "utf-8"
                )
            )
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(content)
            body.extend(crlf)
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        request = urllib.request.Request(
            self._base + method,
            data=bytes(body),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=request_timeout) as response:
                document = json.load(response)
        except Exception as exc:
            raise _transport_error(method, exc) from None
        if not isinstance(document, dict) or not document.get("ok"):
            status = document.get("error_code") if isinstance(document, dict) else None
            raise TelegramError(
                "Telegram API rejected the request",
                operation=_operation(method),
                failure_class="api_rejection" if isinstance(document, dict) else "invalid_response",
                status_code=status if isinstance(status, int) and 100 <= status <= 599 else None,
                retry_after=_retry_after(document),
            )
        return document.get("result")

    def send_document(
        self,
        chat_id: int,
        thread_id: int,
        document_path: Path,
        *,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> int:
        resolved = document_path.expanduser().resolve(strict=False)
        if not resolved.is_file():
            raise TelegramError(
                "document does not exist",
                operation="send_document",
                failure_class="local_validation",
            )
        try:
            content = resolved.read_bytes()
        except OSError:
            raise TelegramError(
                "document cannot be read",
                operation="send_document",
                failure_class="local_io",
            ) from None
        fields: dict[str, str] = {"chat_id": str(chat_id)}
        if thread_id != 1:
            fields["message_thread_id"] = str(thread_id)
        if caption:
            fields["caption"] = caption[:1024]
            fields["parse_mode"] = "HTML"
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        upload_name = file_name or resolved.name
        if (
            not upload_name
            or Path(upload_name).name != upload_name
            or '"' in upload_name
            or "\\" in upload_name
            or any(ord(character) < 32 or ord(character) == 127 for character in upload_name)
        ):
            raise TelegramError(
                "document filename is unsafe",
                operation="send_document",
                failure_class="local_validation",
            )
        detected_mime_type, _ = mimetypes.guess_type(upload_name)
        files = {
            "document": (
                upload_name,
                content,
                mime_type or detected_mime_type or "application/octet-stream",
            )
        }
        result = self._call_multipart(
            "sendDocument", fields=fields, files=files, request_timeout=60.0
        )
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError(
                "sendDocument returned an invalid result",
                operation="send_document",
                failure_class="invalid_response",
            )
        return result["message_id"]
