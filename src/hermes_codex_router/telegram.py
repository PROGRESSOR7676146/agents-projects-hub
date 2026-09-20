from __future__ import annotations

import hashlib
import json
import mimetypes
import os
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

TELEGRAM_HEALTH_FAILURE_THRESHOLD = 3
TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS = 60.0


class TelegramError(RuntimeError):
    _OPERATIONS = frozenset(
        {
            "unknown",
            "api_call",
            "poll",
            "send_message",
            "send_document",
            "get_file",
            "download_file",
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
        "getFile": "get_file",
        "downloadFile": "download_file",
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
class IncomingAttachment:
    kind: str
    file_id: str
    file_unique_id: str
    file_name: str | None
    mime_type: str | None
    file_size: int | None


@dataclass(frozen=True, slots=True)
class DownloadedTelegramFile:
    path: Path
    size: int
    sha256: str


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
    text_source: str = "text"
    attachments: tuple[IncomingAttachment, ...] = ()
    media_group_id: str | None = None
    quote_text: str | None = None
    unavailable_materials: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TopicCallback:
    callback_id: str
    message_id: int
    chat_id: int
    thread_id: int
    sender_id: int
    data: str


def _telegram_nonnegative_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _incoming_materials(
    message: dict[str, Any],
) -> tuple[tuple[IncomingAttachment, ...], tuple[str, ...]]:
    attachments: list[IncomingAttachment] = []
    unavailable: list[str] = []
    document = message.get("document")
    if isinstance(document, dict):
        file_id = document.get("file_id")
        unique_id = document.get("file_unique_id")
        size = document.get("file_size")
        if (
            isinstance(file_id, str)
            and 1 <= len(file_id) <= 512
            and isinstance(unique_id, str)
            and 1 <= len(unique_id) <= 512
        ):
            attachments.append(
                IncomingAttachment(
                    kind="document",
                    file_id=file_id,
                    file_unique_id=unique_id,
                    file_name=(
                        str(document["file_name"])
                        if isinstance(document.get("file_name"), str)
                        else None
                    ),
                    mime_type=(
                        str(document["mime_type"])[:256]
                        if isinstance(document.get("mime_type"), str)
                        else None
                    ),
                    file_size=(
                        size
                        if isinstance(size, int) and not isinstance(size, bool) and size >= 0
                        else None
                    ),
                )
            )
        else:
            unavailable.append("document metadata is incomplete")
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        candidates = [
            item
            for item in photos
            if isinstance(item, dict)
            and isinstance(item.get("file_id"), str)
            and 1 <= len(str(item.get("file_id"))) <= 512
            and isinstance(item.get("file_unique_id"), str)
            and 1 <= len(str(item.get("file_unique_id"))) <= 512
        ]
        if candidates:
            photo = max(
                candidates,
                key=lambda item: (
                    _telegram_nonnegative_int(item.get("file_size")),
                    _telegram_nonnegative_int(item.get("width"))
                    * _telegram_nonnegative_int(item.get("height")),
                ),
            )
            size = photo.get("file_size")
            attachments.append(
                IncomingAttachment(
                    kind="photo",
                    file_id=str(photo["file_id"]),
                    file_unique_id=str(photo["file_unique_id"]),
                    file_name=None,
                    mime_type="image/jpeg",
                    file_size=(
                        size
                        if isinstance(size, int) and not isinstance(size, bool) and size >= 0
                        else None
                    ),
                )
            )
        else:
            unavailable.append("photo metadata is incomplete")
    for field, label in (
        ("animation", "animation"),
        ("audio", "audio"),
        ("video", "video"),
        ("video_note", "video note"),
        ("voice", "voice message"),
        ("sticker", "sticker"),
    ):
        if field in message:
            unavailable.append(f"{label} input is not supported")
    return tuple(attachments), tuple(unavailable)


def _message_text(message: dict[str, Any], *, has_material: bool) -> tuple[str, str] | None:
    text = message.get("text")
    if isinstance(text, str):
        return text, "text"
    caption = message.get("caption")
    if isinstance(caption, str):
        return caption, "caption"
    if has_material:
        return "", "none"
    return None


def _selected_quote(message: dict[str, Any]) -> str | None:
    quote = message.get("quote")
    text = quote.get("text") if isinstance(quote, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()[:4000]


def parse_topic_message(update: dict[str, Any]) -> TopicMessage | None:
    message = update.get("message")
    if not isinstance(message, dict) or message.get("from", {}).get("is_bot"):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    attachments, unavailable_materials = _incoming_materials(message)
    parsed_text = _message_text(message, has_material=bool(attachments or unavailable_materials))
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
        or parsed_text is None
    ):
        return None
    return TopicMessage(
        update_id=int(update["update_id"]),
        message_id=int(message["message_id"]),
        chat_id=int(chat["id"]),
        thread_id=thread_id,
        chat_title=str(chat.get("title") or chat["id"]),
        sender_id=int(sender["id"]),
        text=parsed_text[0],
        reply_to_username=reply_to_username,
        is_forwarded=is_forwarded,
        text_source=parsed_text[1],
        attachments=attachments,
        media_group_id=(
            str(message["media_group_id"])[:256]
            if isinstance(message.get("media_group_id"), str)
            else None
        ),
        quote_text=_selected_quote(message),
        unavailable_materials=unavailable_materials,
    )


def parse_direct_message(update: dict[str, Any]) -> TopicMessage | None:
    """Parse an owner-to-bot private message without treating groups as DMs."""
    message = update.get("message")
    if not isinstance(message, dict) or message.get("from", {}).get("is_bot"):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    attachments, unavailable_materials = _incoming_materials(message)
    parsed_text = _message_text(message, has_material=bool(attachments or unavailable_materials))
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "private"
        or not isinstance(chat.get("id"), int)
        or not isinstance(sender, dict)
        or not isinstance(sender.get("id"), int)
        or chat["id"] != sender["id"]
        or parsed_text is None
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
        text=parsed_text[0],
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
        text_source=parsed_text[1],
        attachments=attachments,
        media_group_id=(
            str(message["media_group_id"])[:256]
            if isinstance(message.get("media_group_id"), str)
            else None
        ),
        quote_text=_selected_quote(message),
        unavailable_materials=unavailable_materials,
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
        normalized_token = token.strip()
        self._base = f"https://api.telegram.org/bot{normalized_token}/"
        self._file_base = f"https://api.telegram.org/file/bot{normalized_token}/"
        self._opener = opener

    def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> DownloadedTelegramFile:
        if not file_id or len(file_id) > 512 or not 1 <= max_bytes <= 2**31:
            raise TelegramError(
                "invalid file download request",
                operation="download_file",
                failure_class="local_validation",
            )
        if destination.is_symlink():
            raise TelegramError(
                "unsafe file download destination",
                operation="download_file",
                failure_class="local_io",
            )
        result = self.call("getFile", file_id=file_id)
        if not isinstance(result, dict):
            raise TelegramError(
                "getFile returned an invalid result",
                operation="get_file",
                failure_class="invalid_response",
            )
        reported_size = result.get("file_size")
        if reported_size is not None and (
            not isinstance(reported_size, int)
            or isinstance(reported_size, bool)
            or reported_size < 0
        ):
            raise TelegramError(
                "getFile returned an invalid size",
                operation="get_file",
                failure_class="invalid_response",
            )
        if (
            isinstance(reported_size, int)
            and not isinstance(reported_size, bool)
            and reported_size > max_bytes
        ):
            raise TelegramError(
                "Telegram file exceeds the configured download limit",
                operation="download_file",
                failure_class="local_validation",
            )
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or not file_path or len(file_path) > 1024:
            raise TelegramError(
                "getFile did not return a safe path",
                operation="get_file",
                failure_class="invalid_response",
            )
        decoded_path = urllib.parse.unquote(file_path)
        parsed_path = urllib.parse.urlsplit(decoded_path)
        segments = decoded_path.split("/")
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or decoded_path.startswith("/")
            or any(not segment or segment in {".", ".."} for segment in segments)
            or "\\" in decoded_path
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        ):
            raise TelegramError(
                "getFile returned an unsafe path",
                operation="get_file",
                failure_class="invalid_response",
            )
        resolved = destination.expanduser().resolve(strict=False)
        resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(resolved.parent, 0o700)
        partial = resolved.with_name(f".{resolved.name}.{uuid.uuid4().hex}.part")
        request = urllib.request.Request(
            self._file_base + urllib.parse.quote(decoded_path, safe="/._-"),
            method="GET",
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                self._opener(request, timeout=TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS) as response,
                partial.open("xb") as output,
            ):
                os.chmod(partial, 0o600)
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramError(
                            "Telegram file exceeds the configured download limit",
                            operation="download_file",
                            failure_class="local_validation",
                        )
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if reported_size is not None and reported_size != size:
                raise TelegramError(
                    "downloaded Telegram file size does not match metadata",
                    operation="download_file",
                    failure_class="invalid_response",
                )
            os.replace(partial, resolved)
            os.chmod(resolved, 0o600)
        except TelegramError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise _transport_error("downloadFile", exc) from None
        return DownloadedTelegramFile(path=resolved, size=size, sha256=digest.hexdigest())

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

    def create_forum_topic(self, chat_id: int, name: str) -> int:
        normalized = " ".join(name.split())
        if chat_id >= 0 or not 1 <= len(normalized) <= 128:
            raise TelegramError(
                "invalid forum topic request",
                operation="create_topic",
                failure_class="local_validation",
            )
        result = self.call("createForumTopic", chat_id=chat_id, name=normalized)
        if not isinstance(result, dict) or not isinstance(result.get("message_thread_id"), int):
            raise TelegramError(
                "createForumTopic returned an invalid result",
                operation="create_topic",
                failure_class="invalid_response",
            )
        thread_id = int(result["message_thread_id"])
        if thread_id <= 0:
            raise TelegramError(
                "createForumTopic returned an invalid result",
                operation="create_topic",
                failure_class="invalid_response",
            )
        return thread_id

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
