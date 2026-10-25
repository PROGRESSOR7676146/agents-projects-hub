"""Read projection of current persisted approval identities, independent of sends."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from .approval_surface import ApprovalWait
from .preacceptance_binding import CURRENT_PREPARED_BINDINGS_SQL, PREPARED_BINDING
from .state_errors import StateError
from .task_activity_binding import ACTIVITY_BINDING, CURRENT_ACTIVITY_BINDINGS_SQL


def _observed_time(value: str | None) -> datetime:
    # Optional observation corruption must not interrupt the operational monitor
    # or manufacture a fresh grace interval. Exact snapshot proof stays separate.
    try:
        parsed = datetime.fromisoformat(value or "")
        if parsed.tzinfo is not None:
            return parsed
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def pending_approval_waits(
    db: sqlite3.Connection, *, now: datetime, selected_profile: Callable[[], str | None]
) -> tuple[tuple[ApprovalWait, ...], bool]:
    """Caller owns one coherent read transaction. No notices or native calls."""
    timestamp = now.isoformat()
    result = []
    rows = db.execute(
        "SELECT a.*,e.identity,e.item_identity,n.created_at AS observed_at "
        "FROM task_activity a JOIN task_activity_entries e ON e.job_id=a.job_id "
        f"JOIN ({CURRENT_ACTIVITY_BINDINGS_SQL}) live ON live.job_id=a.job_id AND "
        + " AND ".join(f"a.{key} IS live.{key}" for key in ACTIVITY_BINDING)
        + " LEFT JOIN task_lifecycle_notices n ON n.event_key='activity:'||a.job_id||':approval:'||e.identity "
        "WHERE e.kind='approval' AND e.state='pending' ORDER BY a.job_id,e.identity LIMIT 257",
        (timestamp,),
    ).fetchall()
    truncated = len(rows) > 256
    for row in rows[:256]:
        # Missing immutable observation time cannot manufacture a new grace interval.
        result.append(
            ApprovalWait(
                row["provider_thread_id"],
                row["provider_turn_id"],
                row["identity"],
                row["item_identity"],
                _observed_time(row["observed_at"]),
            )
        )
    try:
        profile = selected_profile()
    except StateError:
        # Missing configuration cannot prove there are no prepared waits.
        return tuple(result), True
    early = db.execute(
        "SELECT s.*,r.identity,r.item_identity,r.observed_turn_id,r.created_at AS observed_at "
        "FROM preacceptance_scopes s JOIN preacceptance_requests r ON r.scope_id=s.scope_id "
        f"JOIN ({CURRENT_PREPARED_BINDINGS_SQL}) live ON live.job_id=s.job_id AND "
        + " AND ".join(f"s.{key} IS live.{key}" for key in PREPARED_BINDING)
        + " JOIN preacceptance_runtime_epochs epoch ON epoch.slot_key=s.slot_key "
        "AND epoch.epoch=s.epoch AND epoch.instance_token=s.instance_token AND epoch.agent_id=s.agent_id "
        "WHERE s.state='open' AND r.state='pending' "
        "AND (live.provider_turn_id IS NULL OR live.provider_turn_id=r.observed_turn_id) "
        "ORDER BY s.scope_id,r.identity LIMIT ?",
        (timestamp, profile, 257 - len(result)),
    ).fetchall()
    remaining = 256 - len(result)
    truncated |= len(early) > remaining
    for row in early[:remaining]:
        result.append(
            ApprovalWait(
                row["provider_thread_id"],
                row["observed_turn_id"],
                row["identity"],
                row["item_identity"],
                _observed_time(row["observed_at"]),
            )
        )
    return tuple(result), truncated
