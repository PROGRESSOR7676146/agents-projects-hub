"""Read projection of current persisted approval identities, independent of sends."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from .approval_surface import ApprovalWait
from .preacceptance_binding import PREPARED_BINDING, current_prepared_binding, epoch_is_current
from .task_activity_binding import ACTIVITY_BINDING, current_activity_binding


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
        "LEFT JOIN task_lifecycle_notices n ON n.event_key='activity:'||a.job_id||':approval:'||e.identity "
        "WHERE e.kind='approval' AND e.state='pending' ORDER BY a.job_id,e.identity LIMIT 257"
    ).fetchall()
    truncated = len(rows) > 256
    for row in rows[:256]:
        live = current_activity_binding(db, row["job_id"], row["lease_token"], timestamp)
        if live is None or any(row[key] != live[key] for key in ACTIVITY_BINDING):
            continue
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
    early = db.execute(
        "SELECT s.*,r.identity,r.item_identity,r.observed_turn_id,r.created_at AS observed_at "
        "FROM preacceptance_scopes s JOIN preacceptance_requests r ON r.scope_id=s.scope_id "
        "WHERE s.state='open' AND r.state='pending' ORDER BY s.scope_id,r.identity LIMIT 257"
    ).fetchall()
    truncated |= len(early) > 256
    for row in early[:256]:
        if len(result) >= 256:
            truncated = True
            break
        live = current_prepared_binding(
            db, row["job_id"], row["lease_token"], timestamp, selected_profile
        )
        if (
            live is None
            or not epoch_is_current(db, row)
            or any(row[key] != live[key] for key in PREPARED_BINDING)
        ):
            continue
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
