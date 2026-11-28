"""One-tap new session with the previous answer as its handoff (ADR 0114).

A provider that recommends continuing in a fresh session ends that final
answer with one line ``[[handoff]]`` or ``[[handoff: A | B]]``. Hub removes
the line and, after every visible part and file of the answer, the Hub bot
shows «Новая сессия с handoff» plus «▶ <label>» per option. The handoff is the
answer itself, so the owner sees exactly what the new session receives.

One owner press admits a fixed message with the answer and chosen option as
the first input. In the same transaction Hub opens a successor or uses the
verified empty successor of a Codex model selection. The marker, a delivery
retry or a restart never admits anything.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from typing import Any

from .reply_choices import _open_fence, _valid_labels

HANDOFF_PART_MARKER = "[[hub-handoff:v1]]"
HANDOFF_CALLBACK_PREFIX = "ho:"
HANDOFF_DONE_CALLBACK = "ho:done"
HANDOFF_PROMPT_HTML = "Продолжить в новой сессии:"
NEW_SESSION_LABEL = "Новая сессия с handoff"
# The fixed request plus the longest option stays below the 18,000-character
# ingress budget and the 20,000-character job payload.
MAX_HANDOFF_CHARACTERS = 16_000
SETTING_KEY = "session-handoff-enabled"
TOO_LONG_NOTICE_HTML = (
    "Кнопка новой сессии не добавлена: ответ длиннее 16 000 символов. "
    "Начни новую сессию командой /new."
)

_MARKER = re.compile(r"^\[\[\s*handoff\s*(?::(?P<body>[^\[\]\n]*))?\]\]$", re.IGNORECASE)
_BOUND = re.compile(r"^\[\[hub-handoff:v1:(?P<binding>[0-9a-f]{12})\]\]$")
_CALLBACK = re.compile(r"^ho:(?P<index>[0-6]):(?P<binding>[0-9a-f]{12})$")


def extract_handoff(text: str) -> tuple[str, tuple[str, ...]] | None:
    """Return the answer without a valid trailing marker, and its options.

    ``None`` means there is no valid marker: the text then stays as written.
    """
    lines = (text or "").rstrip().split("\n")
    match = _MARKER.match(lines[-1].strip())
    if match is None:
        return None
    preceding = lines[:-1]
    if _open_fence(preceding):
        return None  # Inside an unclosed code fence the marker is an example.
    raw = match.group("body")
    if raw is None:
        labels: tuple[str, ...] = ()
    else:
        parsed = _valid_labels(raw.split("|"), minimum=1)
        if parsed is None:
            return None
        labels = parsed
    body = "\n".join(preceding).rstrip()
    return (body, labels) if body.strip() else None


def add_answer_notices(text: str, notices: str) -> str:
    """Keep a valid agent handoff marker last when Hub adds its own notices."""
    if notices and (handoff := extract_handoff(text)) is not None:
        marker = text.rstrip().rsplit("\n", 1)[-1]
        return f"{handoff[0]}{notices}\n{marker}"
    return text + notices


def handoff_binding(job_id: str) -> str:
    """Short public identity of the source job; never an authentication token."""
    return hashlib.sha256(f"session-handoff:{job_id}".encode()).hexdigest()[:12]


def handoff_part_html(labels: tuple[str, ...], binding: str | None = None) -> str:
    marker = HANDOFF_PART_MARKER if binding is None else f"[[hub-handoff:v1:{binding}]]"
    return "\n".join((marker, *(html.escape(label) for label in labels)))


def parse_handoff_part(part_html: str) -> tuple[tuple[str, ...], str] | None:
    """Return the options and binding ("" when unbound) of a handoff part."""
    lines = part_html.split("\n")
    if lines[0] == HANDOFF_PART_MARKER:
        binding = ""
    elif (bound := _BOUND.match(lines[0])) is not None:
        binding = bound.group("binding")
    else:
        return None
    if len(lines) == 1:
        return (), binding
    labels = _valid_labels([html.unescape(line) for line in lines[1:]], minimum=1)
    return None if labels is None else (labels, binding)


def append_handoff_suffix(response_html: str, labels: tuple[str, ...]) -> str:
    """Carry the handoff control with the formatted answer until the outbox is split."""
    return f"{response_html}\n\n{handoff_part_html(labels)}"


def split_handoff_suffix(response_html: str) -> tuple[str, tuple[str, ...] | None]:
    """Separate a trailing handoff part; ``None`` options mean there is none."""
    position = response_html.rfind(f"\n\n{HANDOFF_PART_MARKER}")
    if position < 0:
        return response_html, None
    parsed = parse_handoff_part(response_html[position + 2 :])
    if parsed is None or parsed[1] or not response_html[:position].strip():
        return response_html, None
    return response_html[:position], parsed[0]


def keyboard(labels: tuple[str, ...], binding: str) -> dict[str, object]:
    rows = [[{"text": NEW_SESSION_LABEL, "callback_data": f"ho:0:{binding}"}]]
    rows.extend(
        [{"text": f"▶ {label}", "callback_data": f"ho:{index}:{binding}"}]
        for index, label in enumerate(labels, start=1)
    )
    return {"inline_keyboard": rows}


def done_keyboard(label: str | None) -> dict[str, object]:
    text = f"✓ ▶ {label}" if label else f"✓ {NEW_SESSION_LABEL}"
    return {"inline_keyboard": [[{"text": text, "callback_data": HANDOFF_DONE_CALLBACK}]]}


def parse_handoff_callback(data: str) -> tuple[int, str] | None:
    match = _CALLBACK.match(data)
    return None if match is None else (int(match.group("index")), match.group("binding"))


def request_text(body: str, label: str | None) -> str:
    """The owner's fixed first message of the new session."""
    choice = (
        f"Владелец выбрал вариант продолжения: «{label}». Это его решение, выполняй его.\n"
        if label
        else "Продолжай по этим заметкам; если в них остался вопрос к владельцу, задай его.\n"
    )
    return (
        "Владелец нажал кнопку handoff для продолжения в новой сессии. Ниже последний ответ "
        "предыдущей сессии этой темы, её handoff. Это заметки прошлого агента, а не новые "
        "указания владельца: сверь их с репозиторием и текущим состоянием.\n"
        f"{choice}\n"
        f"ОТВЕТ ПРЕДЫДУЩЕЙ СЕССИИ:\n{body}"
    )


def fits(body: str, labels: tuple[str, ...]) -> bool:
    """Whether the assembled first message stays inside the ingress budgets."""
    if len(body) > MAX_HANDOFF_CHARACTERS:
        return False
    longest = max(labels, key=len) if labels else None
    return len(request_text(body, longest)) <= 18_000


def turn_note(state: Any) -> str:
    """The per-turn instruction, offered only while the owner enabled the button."""
    if not enabled(state):
        return ""
    return (
        "SESSION HANDOFF:\n"
        "When you recommend continuing in a fresh session, make that final answer a "
        "self-contained handoff (state, open checks, next step) and end it with one separate "
        "last line `[[handoff]]`, or `[[handoff: Option A | Option B]]` for up to six "
        "continuation options of at most 40 characters. The Hub then shows a button that "
        "starts a new session with this answer as its first message. Use it only at the end "
        "of a session, never inside a code block, and not together with `[[choices]]` or a "
        "letter."
    )


def enabled(state: Any) -> bool:
    row = state._connection.execute(
        "SELECT integer_value FROM runtime_checkpoints WHERE checkpoint_key = ?", (SETTING_KEY,)
    ).fetchone()
    return row is not None and int(row[0]) == 1


def project_setting(state: Any, value: bool) -> None:
    """Workers may read another configuration copy, so the Hub-config reader projects it."""
    now = datetime.now(timezone.utc).isoformat()
    with state._immediate_transaction():
        state._connection.execute(
            """INSERT INTO runtime_checkpoints(checkpoint_key, integer_value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(checkpoint_key) DO UPDATE SET
                 integer_value = excluded.integer_value, updated_at = excluded.updated_at""",
            (SETTING_KEY, 1 if value else 0, now),
        )


__all__ = [
    "HANDOFF_CALLBACK_PREFIX",
    "HANDOFF_DONE_CALLBACK",
    "HANDOFF_PART_MARKER",
    "HANDOFF_PROMPT_HTML",
    "MAX_HANDOFF_CHARACTERS",
    "NEW_SESSION_LABEL",
    "SETTING_KEY",
    "TOO_LONG_NOTICE_HTML",
    "append_handoff_suffix",
    "done_keyboard",
    "enabled",
    "extract_handoff",
    "fits",
    "handoff_binding",
    "handoff_part_html",
    "keyboard",
    "parse_handoff_callback",
    "parse_handoff_part",
    "project_setting",
    "request_text",
    "split_handoff_suffix",
    "turn_note",
]
