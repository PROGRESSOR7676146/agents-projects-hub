"""The owner's one-tap decision for an uncertain non-Codex job (ADR 0107).

A worker that proved its own provider process ended adds one final notice
part with a fixed marker and a server-side snapshot of the job's session. The
final sender delivers that part through the Hub bot as a short prompt with two
buttons. The snapshot is a public binding, not an authentication token: the
press is authorized by the configured owner, the delivered Hub message and the
rechecked state, never by the callback data alone.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

PromptAction = Literal["continue", "close"]

PROMPT_HTML = (
    "Что сделать с прерванной задачей? Старый запрос не повторится ни в одном варианте.\n"
    "• <b>Проверить и продолжить</b> — новый ход в той же сессии: сначала проверить, "
    "что уже изменено, затем продолжить только там, где это безопасно.\n"
    "• <b>Закрыть без повтора</b> — итог остаётся неизвестным, проект снимается с паузы."
)
CONTINUE_LABEL = "Проверить и продолжить"
CLOSE_LABEL = "Закрыть без повтора"
DONE_CALLBACK = "ud:done"
DECISION_UNAVAILABLE = "Карточка решения недоступна."

CONTINUATION_PROMPT = (
    "The previous turn in this session ended without a confirmed outcome. Its process was "
    "stopped and may have left partial changes. This is a new owner-requested turn in the "
    "same session, not a replay of the old task. First inspect the current project state and "
    "the changes already made. Explain what remains, then continue only where safe; ask the "
    "owner before any action whose prior completion cannot be determined."
)

_PART = re.compile(r"^\[\[hub-uncertain:v1:(?P<snapshot>[0-9a-f]{16})\]\]$")
_CALLBACK = re.compile(r"^ud:(?P<action>[cx]):(?P<snapshot>[0-9a-f]{16})$")


def snapshot_binding(
    job_id: str, session_id: str, generation: int, provider_session_id: str | None
) -> str:
    """Bind a prompt to the exact job, Hub session, generation and native session."""
    material = (
        f"uncertain-decision:v1:{job_id}:{session_id}:{generation}:{provider_session_id or ''}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def prompt_part_html(snapshot: str) -> str:
    if _PART.match(f"[[hub-uncertain:v1:{snapshot}]]") is None:
        raise ValueError("invalid uncertain decision snapshot")
    return f"[[hub-uncertain:v1:{snapshot}]]"


def parse_prompt_part(part_html: str) -> str | None:
    """Return the snapshot of a stored prompt part, or None for any other part."""
    match = _PART.match(part_html)
    return None if match is None else match.group("snapshot")


def keyboard(snapshot: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": CONTINUE_LABEL, "callback_data": f"ud:c:{snapshot}"}],
            [{"text": CLOSE_LABEL, "callback_data": f"ud:x:{snapshot}"}],
        ]
    }


def done_keyboard(action: PromptAction) -> dict[str, object]:
    label = CONTINUE_LABEL if action == "continue" else CLOSE_LABEL
    return {"inline_keyboard": [[{"text": f"✓ {label}", "callback_data": DONE_CALLBACK}]]}


def parse_callback(data: str) -> tuple[PromptAction, str] | None:
    match = _CALLBACK.match(data)
    if match is None:
        return None
    action: PromptAction = "continue" if match.group("action") == "c" else "close"
    return action, match.group("snapshot")
