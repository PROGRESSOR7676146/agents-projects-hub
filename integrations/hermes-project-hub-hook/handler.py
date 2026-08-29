from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_SOURCE_ROOT = os.getenv("HERMES_PROJECT_HUB_SOURCE")
if not _SOURCE_ROOT:
    _SOURCE_ROOT = str(Path(__file__).resolve().parents[2] / "src")
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

from hermes_codex_router.external_admission import (  # noqa: E402
    acknowledge_unseen_visible_context,
    record_external_turn,
)

_STATE_PATH = Path(
    os.getenv(
        "HERMES_PROJECT_HUB_STATE",
        str(Path.home() / ".local/state/agents-projects-hub/state.db"),
    )
)
_OWNER_USER_IDS = {
    value.strip()
    for value in os.getenv("HERMES_PROJECT_HUB_OWNER_IDS", "").split(",")
    if value.strip()
}


async def handle(event_type: str, context: dict[str, Any]) -> None:
    if event_type != "agent:end" or context.get("platform") != "telegram":
        return
    if str(context.get("user_id") or "") not in _OWNER_USER_IDS:
        return
    try:
        chat_id = int(context.get("chat_id"))
        thread_id = int(context.get("thread_id") or 1)
    except (TypeError, ValueError):
        return
    recorded = record_external_turn(
        _STATE_PATH,
        chat_id=chat_id,
        thread_id=thread_id,
        agent_id="hermes",
        provider_session_id=str(context.get("session_id") or ""),
        model=str(context.get("model") or ""),
        provider=str(context.get("provider") or ""),
        user_excerpt=str(context.get("message") or ""),
        response_excerpt=str(context.get("response") or ""),
    )
    if recorded:
        acknowledge_unseen_visible_context(
            _STATE_PATH,
            chat_id,
            thread_id,
            observer_agent_id="hermes",
        )
