from __future__ import annotations

import html
import re
from contextlib import contextmanager
from typing import Iterator

MAX_PARTIAL_TEXT = 16_000


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
    if isinstance(error, (EOFError, ConnectionError)) or "closed" in message:
        return "connection_lost"
    if isinstance(error, TimeoutError) or "timed out" in message:
        return "timeout"
    return "provider_error"


def codex_failure_notice(error: BaseException) -> str:
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
    notice += (
        "\n\nSaved: Completion is unconfirmed. The task may have changed files or performed "
        "other actions. Hub did not retry it automatically."
    )
    partial = getattr(error, "partial_text", "")
    if isinstance(partial, str) and partial.strip():
        notice += "\n\nPartial response (incomplete):\n" + html.escape(partial[:MAX_PARTIAL_TEXT])
    notice += (
        "\n\nNext: After the provider is available, send a new message: “Inspect the current "
        "project state, summarize what remains, and continue safely.” Hub creates a new job "
        "only from that explicit request."
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
