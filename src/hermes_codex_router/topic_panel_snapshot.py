"""Bounded read-only projection of one topic for the status panel (ADR 0110).

Every value comes from durable Hub evidence through existing predicates. The
reader never writes, never calls providers or models and never reads native
transcripts. The caller holds one read transaction so all queries see the
same SQLite snapshot.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime

from .claude_activity_binding import latest_claude_visible
from .delivery_control_predicates import final_control_reconciled
from .delivery_hold_predicates import nonblocking_execution_notice, outbox_delivery_hold_released
from .root_blockers import persistent_root_blocker
from .topic_panel_view import ActivityKind, Headline, LastResult, OwnerReason, PanelSnapshot

_RUNNING = ("leased", "executing")


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else None


def _blocking_unknown_delivery(connection: sqlite3.Connection, topic_id: int) -> bool:
    # The same "outstanding" rule as DeliveryStateFacade.topic_delivery_controls.
    row = connection.execute(
        f"""SELECT 1 FROM telegram_outbox o JOIN provider_jobs j ON j.job_id=o.job_id
            WHERE j.topic_id=? AND o.status='unknown'
              AND NOT ({outbox_delivery_hold_released("o")} OR {final_control_reconciled("o")}
                       OR {nonblocking_execution_notice("o")})
            LIMIT 1""",
        (topic_id,),
    ).fetchone()
    return row is not None


def _activity(
    connection: sqlite3.Connection, job: sqlite3.Row, now: datetime
) -> tuple[ActivityKind | None, datetime | None, bool]:
    """Exact-bound agent activity of the running job; process liveness is never used."""
    job_id = str(job["job_id"])
    codex = connection.execute(
        "SELECT mode,last_meaningful_at FROM task_activity WHERE job_id=? AND lease_token=?",
        (job_id, job["lease_token"]),
    ).fetchone()
    if codex is not None:
        mode = str(codex["mode"])
        kind: ActivityKind | None = (
            "approval" if mode == "approval" else "tool" if mode == "tool" else None
        )
        return kind, _time(codex["last_meaningful_at"]), mode == "approval"
    waiting = (
        connection.execute(
            """SELECT 1 FROM claude_permission_requests request
               JOIN claude_permission_launches launch USING(launch_epoch)
               WHERE launch.job_id=? AND request.status='pending' AND request.expires_at>?
               LIMIT 1""",
            # The permission journal stores expiry as integer epoch milliseconds.
            (job_id, int(now.timestamp() * 1000)),
        ).fetchone()
        is not None
    )
    visible = latest_claude_visible(connection, job_id)
    visible_at = _time(visible[1]) if visible is not None else None
    # A Claude permission request has no durable creation time: the wait is an
    # owner reason only and never relabels the last visible message's time.
    return ("message" if visible_at is not None else None), visible_at, waiting


def read_panel_snapshot(
    connection: sqlite3.Connection,
    *,
    topic_id: int,
    now: datetime,
    agent_names: Mapping[str, str],
) -> PanelSnapshot:
    """Project one topic; ``now`` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("panel snapshot time must be timezone-aware")
    reasons: list[OwnerReason] = []
    blocker = persistent_root_blocker(connection, topic_id=topic_id)
    if blocker is not None and blocker.kind == "uncertain" and blocker.topic_id == topic_id:
        reasons.append("uncertain")
    if _blocking_unknown_delivery(connection, topic_id):
        reasons.append("delivery_unknown")
    held = int(
        connection.execute(
            """SELECT COUNT(*) FROM provider_job_holds h JOIN provider_jobs j USING(job_id)
               WHERE j.topic_id=? AND h.decision='pending'""",
            (topic_id,),
        ).fetchone()[0]
    )
    queued = int(
        connection.execute(
            """SELECT COUNT(*) FROM provider_jobs j WHERE j.topic_id=?
               AND j.status IN ('queued','retry_wait')
               AND NOT EXISTS (SELECT 1 FROM provider_job_holds h
                               WHERE h.job_id=j.job_id AND h.decision='pending')""",
            (topic_id,),
        ).fetchone()[0]
    )
    job = connection.execute(
        f"""SELECT * FROM provider_jobs WHERE topic_id=? AND status IN {_RUNNING}
            ORDER BY topic_sequence LIMIT 1""",
        (topic_id,),
    ).fetchone()
    agent = model = None
    started_at = activity_at = interim_at = None
    kind: ActivityKind | None = None
    identity = (
        job
        if job is not None
        else connection.execute(
            """SELECT agent_id,model FROM provider_jobs j WHERE j.topic_id=?
           AND j.status IN ('queued','retry_wait')
           AND NOT EXISTS (SELECT 1 FROM provider_job_holds h
                           WHERE h.job_id=j.job_id AND h.decision='pending')
           ORDER BY j.topic_sequence LIMIT 1""",
            (topic_id,),
        ).fetchone()
    )
    if identity is not None:
        agent = agent_names.get(str(identity["agent_id"]), str(identity["agent_id"]))
        model = str(identity["model"]) if identity["model"] else None
    if job is not None:
        if job["status"] == "executing":
            started_at = _time(job["provider_started_at"])
            kind, activity_at, waiting = _activity(connection, job, now)
            if waiting:
                reasons.append("approval")
            interim = connection.execute(
                """SELECT MAX(delivered_at) FROM provider_progress_deliveries
                   WHERE job_id=? AND status='delivered'""",
                (job["job_id"],),
            ).fetchone()[0]
            interim_at = _time(interim)
    stop = connection.execute(
        "SELECT 1 FROM provider_stop_requests WHERE topic_id=? AND status='pending' LIMIT 1",
        (topic_id,),
    ).fetchone()
    headline: Headline
    if reasons:
        headline = "needs_owner"
    elif stop is not None:
        headline = "stop_requested"
    elif blocker is not None and blocker.kind in ("local", "terminal"):
        headline = "local_writer"
    elif job is not None:
        headline = "executing" if job["status"] == "executing" else "preparing"
    elif queued:
        headline = "queued"
    else:
        headline = "idle"
    last_result: LastResult | None = None
    last_result_at = None
    if headline == "idle":
        saved = connection.execute(
            """SELECT 1 FROM provider_jobs WHERE topic_id=? AND status='result_ready' LIMIT 1""",
            (topic_id,),
        ).fetchone()
        delivered = connection.execute(
            """SELECT MAX(o.delivered_at) FROM telegram_outbox o
               JOIN provider_jobs j ON j.job_id=o.job_id
               WHERE j.topic_id=? AND o.status='delivered' AND o.purpose='provider_result'""",
            (topic_id,),
        ).fetchone()[0]
        if saved is not None:
            last_result = "saved"
        elif delivered is not None:
            last_result, last_result_at = "delivered", _time(delivered)
    return PanelSnapshot(
        observed_at=now,
        headline=headline,
        owner_reasons=tuple(reasons),
        agent=agent,
        model=model,
        started_at=started_at,
        activity_kind=kind,
        activity_at=activity_at,
        interim_delivered_at=interim_at,
        queued=queued,
        held=held,
        last_result=last_result,
        last_result_at=last_result_at,
    )
