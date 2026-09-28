"""Test-only reads of emergency stop requests (ADR 0046)."""

from __future__ import annotations

from hermes_codex_router.state import HubState


def pending_stop(state: HubState, topic_id: int, agent_id: str) -> str | None:
    """The oldest pending stop in the topic addressed to this provider."""
    row = state._connection.execute(
        """SELECT request_id FROM provider_stop_requests
           WHERE topic_id = ? AND target_agent_id = ? AND status = 'pending'
           ORDER BY created_at LIMIT 1""",
        (topic_id, agent_id),
    ).fetchone()
    return None if row is None else str(row["request_id"])
