"""Dependency-neutral reads of the existing emergency-stop coverage rule."""

from __future__ import annotations

import sqlite3


def stop_covers(stop: str, job: str) -> str:
    """SQL for trusted internal aliases; held and later work is excluded."""
    return f"""{stop}.topic_id = {job}.topic_id AND {job}.created_at <= {stop}.created_at
     AND NOT EXISTS (
       SELECT 1 FROM provider_job_holds held
       WHERE held.job_id = {job}.job_id AND held.held_at <= {stop}.created_at
         AND (held.decision = 'pending' OR held.decided_at > {stop}.created_at)
     )"""


STOP_COVERS_JOB_SQL = stop_covers("stop", "job")

_PENDING_STOP_FOR_JOB_SQL = f"""SELECT stop.request_id FROM provider_stop_requests stop
   JOIN provider_jobs job ON job.job_id = ?
   WHERE stop.status = 'pending' AND {STOP_COVERS_JOB_SQL}
   ORDER BY stop.created_at LIMIT 1"""


def pending_stop_for_job(db: sqlite3.Connection, job_id: str) -> str | None:
    row = db.execute(_PENDING_STOP_FOR_JOB_SQL, (job_id,)).fetchone()
    return None if row is None else str(row["request_id"])
