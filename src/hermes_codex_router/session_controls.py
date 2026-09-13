"""Generation-bound controls for topics retaining adopted-session evidence."""

from __future__ import annotations

import hashlib

from .state import HubState, StateError


def requires_bound_controls(state: HubState, topic_id: int) -> bool:
    return (
        state._connection.execute(
            """SELECT 1 FROM codex_session_origins o JOIN agent_sessions s
           ON s.session_id=o.session_id WHERE s.topic_id=? LIMIT 1""",
            (topic_id,),
        ).fetchone()
        is not None
    )


def _stamp(topic_id: int, session_id: str) -> str:
    # This is a generation identity, not an authentication token. Telegram owner
    # authorization and cached model/effort validation remain mandatory.
    return hashlib.sha256(f"{topic_id}:{session_id}".encode()).hexdigest()[:16]


def bind_controls(
    state: HubState, topic_id: int, values: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    if not requires_bound_controls(state, topic_id):
        return values
    current = state.active_session(topic_id)
    stamp = _stamp(topic_id, current.session_id if current else "")
    result = []
    for label, data in values:
        # Reset already carries a complete session ID and is compare-and-swap.
        encoded = data if data.startswith("new:") else f"{data}~{stamp}"
        if len(encoded.encode()) > 64:
            raise StateError("session control exceeds Telegram callback bound")
        result.append((label, encoded))
    return result


def validate_control(state: HubState, topic_id: int, data: str) -> tuple[str, str]:
    current = state.active_session(topic_id)
    session_id = current.session_id if current else ""
    if "~" in data:
        action, stamp = data.rsplit("~", 1)
        if stamp != _stamp(topic_id, session_id):
            raise StateError("active session changed; open controls again")
        return action, session_id
    if requires_bound_controls(state, topic_id) and not data.startswith("new:"):
        raise StateError("old controls cannot change this session; open controls again")
    return data, session_id
