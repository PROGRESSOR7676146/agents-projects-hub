from __future__ import annotations

import html
import re
from contextlib import contextmanager
from typing import Iterator

MAX_PARTIAL_TEXT = 10_000


class CodexPreparationError(RuntimeError):
    """Caught setup failure before the productive turn/start call was made."""


@contextmanager
def codex_preparation() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        raise CodexPreparationError(str(exc)) from exc


def codex_failure_reason(error: BaseException) -> str:
    """Classify a cause for display; this never proves side-effect safety."""
    message = str(error)[:2000].lower()
    if re.search(r"\b429\b|too many requests|usage limit|rate.?limit", message):
        return "rate_limited"
    if (
        isinstance(error, (EOFError, ConnectionError))
        or "closed" in message
        or "disconnected" in message
    ):
        return "connection_lost"
    if isinstance(error, TimeoutError) or "timed out" in message:
        return "timeout"
    return "provider_error"


def codex_failure_notice(
    error: BaseException, *, turn_status: str = "unknown", held_count: int = 0
) -> str:
    """Only fixed causes and explicitly visible assistant text reach Telegram."""
    if isinstance(error, CodexPreparationError):
        return (
            "What happened: Codex could not prepare the task before starting it.\n\n"
            "Saved: No productive provider turn was sent.\n\n"
            "Next: Retry after Codex is available."
        )
    reason = getattr(error, "failure_reason", codex_failure_reason(error))
    causes = {
        "rate_limited": "Codex stopped after a provider rate-limit error (429 or usage limit).",
        "connection_lost": "Hub lost the connection to Codex before confirming completion.",
        "timeout": "Hub timed out waiting for Codex to confirm completion.",
    }
    notice = "What happened: " + causes.get(
        reason, "Codex stopped before Hub could confirm completion."
    )
    if turn_status in {"failed", "interrupted"}:
        notice += (
            "\n\nSaved: The exact Codex turn has stopped with a terminal error. "
            "Its outcome is incomplete; files or external state may already have changed. "
            "Hub kept the partial response and did not replay the task."
        )
    else:
        notice += (
            "\n\nSaved: Completion and turn activity are unconfirmed. The task may have "
            "changed files or performed other actions. Hub did not retry it."
        )
    partial = getattr(error, "partial_text", "")
    if isinstance(partial, str) and partial.strip():
        notice += "\n\nPartial response (incomplete):\n" + html.escape(partial[:MAX_PARTIAL_TEXT])
    if turn_status in {"failed", "interrupted"}:
        notice += (
            "\n\nNext: Continue with inspection — reply exactly retry to this notice. "
            "Hub will start a new turn in the same session to inspect current project state "
            "and prior changes before continuing. If local CLI owns the session, close it "
            "and use /return first."
        )
        if held_count:
            notice += f" {held_count} earlier queued request(s) remain paused for review."
    else:
        notice += (
            "\n\nNext: Hub keeps this root paused while the exact turn status is unknown. "
            "Check /status and wait for read-only reconciliation; do not repeat the task."
        )
    return notice


def uncertain_provider_notice(display_name: str) -> str:
    """Explain an uncertain non-Codex outcome without exposing provider diagnostics."""
    safe_name = html.escape(display_name)
    return (
        f"What happened: {safe_name} stopped before a final response.\n\n"
        "Saved: Completion is unconfirmed. The task may have changed files or performed "
        "other actions. Hub did not retry it automatically.\n\n"
        f"Next: Send a new message asking {safe_name} to inspect the current project state, "
        "summarize what remains, and continue safely. Hub creates a new job only from that "
        "explicit request."
    )
