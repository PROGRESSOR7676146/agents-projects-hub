"""Closed reply choices that a provider offers as Telegram inline buttons.

A provider ends its visible answer with one line ``[[choices: A | B | C]]``.
Hub removes that line and delivers the labels as a separate final part whose
HTML starts with a fixed marker; the sender turns that part into buttons. A
pressed button becomes the owner's ordinary reply with the label as text, so
choices grant no authority beyond typing the same words. Anything malformed
stays visible as plain text.
"""

from __future__ import annotations

import html
import re

MIN_CHOICES = 2
MAX_CHOICES = 6
MAX_LABEL_CHARACTERS = 40
CHOICE_PART_MARKER = "[[hub-choices:v1]]"
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
    if len({label.casefold() for label in labels}) != len(labels):
        return None
    return labels


def extract_reply_choices(text: str) -> tuple[str, tuple[str, ...]]:
    """Return the answer without a valid trailing marker, and its labels."""
    lines = text.rstrip().split("\n")
    match = _MARKER.match(lines[-1].strip())
    if match is None:
        return text, ()
    preceding = lines[:-1]
    if sum(line.lstrip().startswith("```") for line in preceding) % 2:
        # The marker is inside an unclosed code fence: it is example content.
        return text, ()
    labels = _valid_labels(match.group("body").split("|"))
    if labels is None:
        return text, ()
    return "\n".join(preceding).rstrip(), labels


def choice_part_html(labels: tuple[str, ...]) -> str:
    return "\n".join((CHOICE_PART_MARKER, *(html.escape(label) for label in labels)))


def parse_choice_part(part_html: str) -> tuple[str, ...] | None:
    lines = part_html.split("\n")
    if lines[0] != CHOICE_PART_MARKER:
        return None
    return _valid_labels([html.unescape(line) for line in lines[1:]])
