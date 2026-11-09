"""Closed reply choices that a provider offers as Telegram inline buttons.

A provider ends its visible answer with one line ``[[choices: A | B | C]]``.
Hub removes that line and delivers the labels as a separate final part whose
HTML starts with a fixed marker; the sender turns that part into buttons. A
pressed button becomes the owner's ordinary reply with the label as text, so
choices grant no authority beyond typing the same words. Anything malformed
stays visible as plain text. Each stored part is bound to the Hub session of its
job, so buttons from an earlier session cannot answer the current one.
"""

from __future__ import annotations

import hashlib
import html
import re

MIN_CHOICES = 2
MAX_CHOICES = 6
MAX_LABEL_CHARACTERS = 40
CHOICE_PART_MARKER = "[[hub-choices:v1]]"
_BOUND = re.compile(r"^\[\[hub-choices:v1:(?P<binding>[0-9a-f]{12})\]\]$")
_CALLBACK = re.compile(r"^ch:(?P<index>[0-9]):(?P<binding>[0-9a-f]{12})$")
CHOICE_PROMPT_HTML = "Выберите вариант:"

_MARKER = re.compile(r"^\[\[\s*choices\s*:(?P<body>[^\[\]\n]*)\]\]$", re.IGNORECASE)


def _valid_labels(raw: list[str]) -> tuple[str, ...] | None:
    labels = tuple(label.strip() for label in raw)
    if not MIN_CHOICES <= len(labels) <= MAX_CHOICES:
        return None
    if any(not label or len(label) > MAX_LABEL_CHARACTERS for label in labels):
        return None
    if any(not label.isprintable() for label in labels):
        return None
    # A pressed label is sent as text; a slash would make it a Hub command.
    if any(label.startswith("/") for label in labels):
        return None
    if len({label.casefold() for label in labels}) != len(labels):
        return None
    return labels


_FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _open_fence(lines: list[str]) -> bool:
    """Whether Markdown text ends inside a fenced code block (CommonMark fences)."""
    opened: str | None = None
    for line in lines:
        match = _FENCE.match(line)
        if match is None:
            continue
        fence = match.group("fence")
        if opened is None:
            if not (fence[0] == "`" and "`" in match.group("info")):
                opened = fence
        elif (
            fence[0] == opened[0] and len(fence) >= len(opened) and not match.group("info").strip()
        ):
            opened = None
    return opened is not None


def extract_reply_choices(text: str) -> tuple[str, tuple[str, ...]]:
    """Return the answer without a valid trailing marker, and its labels."""
    lines = text.rstrip().split("\n")
    match = _MARKER.match(lines[-1].strip())
    if match is None:
        return text, ()
    preceding = lines[:-1]
    if _open_fence(preceding):
        # The marker is inside an unclosed code fence: it is example content.
        return text, ()
    labels = _valid_labels(match.group("body").split("|"))
    if labels is None:
        return text, ()
    return "\n".join(preceding).rstrip(), labels


def session_binding(session_id: str) -> str:
    """Short public identity of one Hub session; not an authentication token."""
    return hashlib.sha256(f"reply-choices:{session_id}".encode()).hexdigest()[:12]


def choice_part_html(labels: tuple[str, ...], binding: str | None = None) -> str:
    marker = CHOICE_PART_MARKER if binding is None else f"[[hub-choices:v1:{binding}]]"
    return "\n".join((marker, *(html.escape(label) for label in labels)))


def parse_choice_part(part_html: str) -> tuple[tuple[str, ...], str] | None:
    """Return labels and session binding ("" when unbound) of a choice part."""
    lines = part_html.split("\n")
    if lines[0] == CHOICE_PART_MARKER:
        binding = ""
    elif (bound := _BOUND.match(lines[0])) is not None:
        binding = bound.group("binding")
    else:
        return None
    labels = _valid_labels([html.unescape(line) for line in lines[1:]])
    return None if labels is None else (labels, binding)


def parse_choice_callback(data: str) -> tuple[int, str] | None:
    match = _CALLBACK.match(data)
    return None if match is None else (int(match.group("index")), match.group("binding"))


def keyboard(labels: tuple[str, ...], binding: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"ch:{index}:{binding}"}]
            for index, label in enumerate(labels)
        ]
    }


def append_choice_suffix(response_html: str, labels: tuple[str, ...]) -> str:
    """Carry labels with the formatted answer until the outbox is split."""
    if not labels:
        return response_html
    return f"{response_html}\n\n{choice_part_html(labels)}"


def split_choice_suffix(response_html: str) -> tuple[str, tuple[str, ...]]:
    """Separate a trailing choice part from the visible answer HTML."""
    position = response_html.rfind(f"\n\n{CHOICE_PART_MARKER}\n")
    if position < 0:
        return response_html, ()
    parsed = parse_choice_part(response_html[position + 2 :])
    if parsed is None or parsed[1] or not response_html[:position].strip():
        return response_html, ()
    return response_html[:position], parsed[0]
