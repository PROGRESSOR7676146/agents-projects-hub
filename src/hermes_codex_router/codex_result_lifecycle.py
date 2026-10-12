"""Optional telemetry and socket retirement after successful result publication."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .codex_appserver import CodexAppServerClient, RateLimits, TurnResult, context_remaining_percent
from .diagnostic_log import survived

if TYPE_CHECKING:
    from .state import HubState


@dataclass(frozen=True, slots=True)
class InlineCodexTurn:
    text: str
    client: CodexAppServerClient
    thread_id: str
    turn_id: str


def post_completion_context(state: HubState, session_id: str, result: TurnResult) -> None:
    try:
        state.set_context_remaining(session_id, context_remaining_percent(result))
    except Exception as error:
        survived("codex_result_lifecycle.context", error)


def post_completion_limits(client: CodexAppServerClient) -> RateLimits:
    try:
        return client.read_rate_limits(deadline=time.monotonic() + 5.0)
    except Exception as error:
        survived("codex_result_lifecycle.telemetry", error)
        return RateLimits(None, None)


def retire_completed_connection(
    client: CodexAppServerClient,
    *,
    thread_id: str,
    turn_id: str,
    retire: Callable[[], None],
    warning: Callable[[str, str], None],
) -> None:
    """The caller has published successfully; cleanup never reclassifies that result."""
    try:
        if not client.consume_completed_connection(thread_id=thread_id, turn_id=turn_id):
            return
        retire()
    except Exception as error:
        survived("codex_result_lifecycle.retirement", error)
        try:
            warning(
                "completed_socket_retirement_error",
                "The completed result is saved; closure of its retired socket was not confirmed.",
            )
        except Exception as report_error:
            survived("codex_result_lifecycle.warning", report_error)
