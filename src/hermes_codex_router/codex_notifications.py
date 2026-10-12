from __future__ import annotations

from typing import Any


def retain_turn_notification(
    message: dict[str, Any], *, thread_id: str | None, turn_id: str | None
) -> bool:
    """Buffer only events consumed by the current turn's result collector.

    Activity is normalized separately, without retaining tool/reasoning payloads.
    During start acceptance the turn ID is unknown; wait validates it afterwards.
    Older protocol events may omit threadId, but an explicit mismatch is rejected.
    """
    if thread_id is None:
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    if "threadId" in params and params["threadId"] != thread_id:
        return False
    method = message.get("method")
    event_turn = params.get("turnId")
    if method == "turn/completed":
        turn = params.get("turn")
        event_turn = turn.get("id") if isinstance(turn, dict) else None
    elif method == "item/completed":
        item = params.get("item")
        if not (
            isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and isinstance(item.get("text"), str)
        ):
            return False
    elif method not in {"thread/tokenUsage/updated", "error"}:
        return False
    return (
        isinstance(event_turn, str)
        and bool(event_turn)
        and (turn_id is None or event_turn == turn_id)
    )
