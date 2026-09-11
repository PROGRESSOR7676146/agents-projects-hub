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
            "Codex failed during preparation, before starting the task. "
            "No productive turn was sent. The provider must be available before retrying."
        )
    reason = getattr(error, "failure_reason", codex_failure_reason(error))
    causes = {
        "rate_limited": "Codex stopped after a provider rate-limit error (429 or usage limit).",
        "connection_lost": "Hub lost the connection to Codex before confirming completion.",
        "timeout": "Hub timed out waiting for Codex to confirm completion.",
    }
    notice = causes.get(reason, "Codex stopped before Hub could confirm completion.")
    notice += (
        " Completion is unconfirmed; the task may have changed files or performed other actions. "
        "Hub did not run it again automatically."
    )
    partial = getattr(error, "partial_text", "")
    if isinstance(partial, str) and partial.strip():
        notice += "\n\nSaved partial response (incomplete):\n" + html.escape(
            partial[:MAX_PARTIAL_TEXT]
        )
    notice += "\n\nYou can ask Codex to check the partial work and continue when the provider is available."
    return notice
