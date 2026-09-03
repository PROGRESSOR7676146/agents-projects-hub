from __future__ import annotations

import re
from typing import Protocol

TELEGRAM_HTML_LIMIT = 4090

_TOKEN = re.compile(r"(<[^>]+>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);)")
_OPEN_TAG = re.compile(r"<\s*([A-Za-z0-9-]+)(?:\s[^>]*)?>$")
_CLOSE_TAG = re.compile(r"</\s*([A-Za-z0-9-]+)\s*>$")


class TelegramHtmlSender(Protocol):
    def send_html(self, chat_id: int, thread_id: int, html: str) -> int: ...


def split_telegram_html(value: str, *, limit: int = TELEGRAM_HTML_LIMIT) -> tuple[str, ...]:
    """Split Telegram HTML into independently valid, ordered messages.

    The splitter is deliberately conservative and counts encoded HTML bytes as
    visible characters.  It never cuts a tag or entity and balances formatting
    tags at every boundary so each part can be retried independently.
    """
    if limit < 64:
        raise ValueError("Telegram HTML part limit is too small")
    text = value.strip()
    if not text:
        raise ValueError("Telegram HTML cannot be empty")
    if len(text) <= limit:
        return (text,)

    tokens = [token for token in _TOKEN.split(text) if token]
    parts: list[str] = []
    current = ""
    open_tags: list[tuple[str, str]] = []

    def closing_suffix(tags: list[tuple[str, str]] | None = None) -> str:
        active = open_tags if tags is None else tags
        return "".join(f"</{name}>" for name, _opening in reversed(active))

    def flush() -> None:
        nonlocal current
        if not current:
            return
        suffix = closing_suffix()
        part = f"{current}{suffix}"
        if part.strip():
            parts.append(part)
        current = "".join(opening for _name, opening in open_tags)

    for token in tokens:
        closing = _CLOSE_TAG.fullmatch(token)
        opening = _OPEN_TAG.fullmatch(token) if not closing else None
        self_closing = token.rstrip().endswith("/>")

        if closing:
            prospective_tags = list(open_tags)
            name = closing.group(1).casefold()
            if prospective_tags and prospective_tags[-1][0] == name:
                prospective_tags.pop()
            if len(current) + len(token) + len(closing_suffix(prospective_tags)) > limit:
                flush()
            current += token
            if open_tags and open_tags[-1][0] == name:
                open_tags.pop()
            continue

        if opening or token.startswith("&"):
            prospective_tags = list(open_tags)
            if opening and not self_closing:
                prospective_tags.append((opening.group(1).casefold(), token))
            if len(current) + len(token) + len(closing_suffix(prospective_tags)) > limit:
                flush()
            current += token
            if opening and not self_closing:
                open_tags.append((opening.group(1).casefold(), token))
            continue

        remaining = token
        while remaining:
            capacity = limit - len(current) - len(closing_suffix())
            if capacity <= 0:
                flush()
                capacity = limit - len(current) - len(closing_suffix())
            if len(remaining) <= capacity:
                current += remaining
                break
            cut = capacity
            preferred = max(
                remaining.rfind("\n", 0, capacity),
                remaining.rfind(" ", 0, capacity),
            )
            if preferred >= max(1, capacity // 2):
                cut = preferred + 1
            current += remaining[:cut]
            remaining = remaining[cut:]
            flush()

    flush()
    if not parts or any(len(part) > limit for part in parts):
        raise ValueError("Telegram HTML could not be split safely")
    return tuple(parts)


def send_telegram_html_parts(
    telegram: TelegramHtmlSender, chat_id: int, thread_id: int, html: str
) -> tuple[int, ...]:
    """Send all parts in order for legacy immediate-delivery paths."""
    return tuple(telegram.send_html(chat_id, thread_id, part) for part in split_telegram_html(html))
