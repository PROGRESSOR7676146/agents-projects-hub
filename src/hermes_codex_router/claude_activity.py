"""Optional process observations over mandatory durable Claude journal evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone

from .claude_activity_binding import (
    CLAUDE_ACTIVITY_BINDING,
    claude_permission_snapshot,
    current_claude_binding,
    latest_claude_visible,
)
from .task_lifecycle import TaskLifecycleNotice, TaskLifecycleState

NOTICE_COPY = (
    "No new completed visible messages have been observed from Claude. "
    "The task's outcome is not confirmed. Check /status or use /stop in this topic."
)


class ClaudeActivityState:
    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
        state_error: Callable[[str], Exception],
        notices: TaskLifecycleState,
        notices_enabled: bool = False,
    ) -> None:
        self.db, self.transaction, self.state_error = db, transaction, state_error
        self.notices, self.notices_enabled = notices, notices_enabled

    def _time(self, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise self.state_error("Claude observation timestamp must be timezone-aware")
        return now.astimezone(timezone.utc).isoformat()

    def open_process_observation(
        self,
        job_id: str,
        token: str,
        native_session_id: str,
        project_root: str,
        *,
        now: datetime,
    ) -> bool:
        timestamp = self._time(now)
        with self.transaction():
            live = current_claude_binding(self.db, job_id, token, timestamp)
            if live is None or (live["native_session_id"], live["project_root"]) != (
                native_session_id,
                project_root,
            ):
                raise self.state_error("Claude process observation requires exact live execution")
            permission = claude_permission_snapshot(self.db, live, timestamp)
            visible = latest_claude_visible(self.db, job_id)
            if permission is None or visible is None:
                raise self.state_error("Claude observation evidence is unavailable")
            prior = self.db.execute(
                "SELECT * FROM claude_activity_observations WHERE job_id=?", (job_id,)
            ).fetchone()
            if prior is not None:
                if any(prior[key] != live[key] for key in CLAUDE_ACTIVITY_BINDING):
                    raise self.state_error("Claude observation binding changed")
                return False
            fields = (
                "job_id",
                *CLAUDE_ACTIVITY_BINDING,
                "permission_launch_epoch",
                "process_observed_at",
                "last_visible_sequence",
                "quiet_since_at",
                "permission_snapshot_digest",
                "permission_waiting",
            )
            values = (
                job_id,
                *(live[key] for key in CLAUDE_ACTIVITY_BINDING),
                permission.launch_epoch,
                timestamp,
                visible[0],
                max(timestamp, visible[1] or timestamp),
                permission.digest,
                int(permission.waiting),
            )
            self.db.execute(
                "INSERT INTO claude_activity_observations ("
                + ",".join(fields)
                + ") VALUES ("
                + ",".join("?" for _ in fields)
                + ")",
                values,
            )
            return True

    def _supersede(self, job_id: str, timestamp: str) -> None:
        self.db.execute(
            "UPDATE task_lifecycle_notices SET status='superseded',"
            "lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
            "WHERE job_id=? AND kind='claude_no_progress' AND status IN ('pending','leased') "
            "AND attempt_count=0 AND send_started_at IS NULL",
            (timestamp, job_id),
        )

    def retire(self, job_id: str, token: str, *, now: datetime) -> None:
        timestamp = self._time(now)
        with self.transaction():
            changed = self.db.execute(
                "UPDATE claude_activity_observations SET retired_at=COALESCE(retired_at,?) "
                "WHERE job_id=? AND lease_token=?",
                (timestamp, job_id, token),
            ).rowcount
            if changed:
                self._supersede(job_id, timestamp)

    def evaluate(
        self, *, now: datetime, ordinary_seconds: int = 300
    ) -> tuple[TaskLifecycleNotice, ...]:
        timestamp = self._time(now)
        if type(ordinary_seconds) is not int or not 1 <= ordinary_seconds <= 86400:
            raise self.state_error("Claude observation threshold must be 1..86400 seconds")
        if not self.notices_enabled:
            return ()
        result = []
        with self.transaction():
            rows = self.db.execute(
                "SELECT observed.* FROM claude_activity_observations observed "
                "JOIN provider_jobs job ON job.job_id=observed.job_id "
                "AND job.status='executing' AND job.lease_token=observed.lease_token "
                "AND job.lease_expires_at>? WHERE observed.retired_at IS NULL "
                "ORDER BY observed.quiet_since_at,observed.job_id LIMIT 256",
                (timestamp,),
            ).fetchall()
            for row in rows:
                live = current_claude_binding(self.db, row["job_id"], row["lease_token"], timestamp)
                if live is None or any(row[key] != live[key] for key in CLAUDE_ACTIVITY_BINDING):
                    continue
                visible = latest_claude_visible(self.db, row["job_id"])
                permission = claude_permission_snapshot(self.db, live, timestamp)
                if (
                    visible is None
                    or visible[0] < row["last_visible_sequence"]
                    or permission is None
                    or permission.launch_epoch != row["permission_launch_epoch"]
                ):
                    self.db.execute(
                        "UPDATE claude_activity_observations SET retired_at=? WHERE job_id=?",
                        (timestamp, row["job_id"]),
                    )
                    self._supersede(row["job_id"], timestamp)
                    continue
                quiet = row["quiet_since_at"]
                changed = False
                if visible[0] != row["last_visible_sequence"]:
                    quiet = max(quiet, visible[1] or timestamp)
                    changed = True
                if permission.digest != row[
                    "permission_snapshot_digest"
                ] or permission.waiting != bool(row["permission_waiting"]):
                    quiet = max(quiet, timestamp)
                    changed = True
                episode = row["episode"] + int(changed)
                if changed:
                    self._supersede(row["job_id"], timestamp)
                    self.db.execute(
                        "UPDATE claude_activity_observations SET quiet_since_at=?,last_visible_sequence=?,"
                        "permission_snapshot_digest=?,permission_waiting=?,episode=?,notified_episode=NULL "
                        "WHERE job_id=?",
                        (
                            quiet,
                            visible[0],
                            permission.digest,
                            int(permission.waiting),
                            episode,
                            row["job_id"],
                        ),
                    )
                if permission.waiting or (not changed and row["notified_episode"] == episode):
                    continue
                if (now - datetime.fromisoformat(quiet)).total_seconds() < ordinary_seconds:
                    continue
                notice, _ = self.notices.prepare_notice_in_transaction(
                    event_key=f"claude-observation:{row['job_id']}:no-progress:{episode}",
                    kind="claude_no_progress",
                    job_id=row["job_id"],
                    chat_id=row["chat_id"],
                    thread_id=row["thread_id"],
                    telegram_html=NOTICE_COPY,
                    now=now,
                )
                self.db.execute(
                    "UPDATE claude_activity_observations SET notified_episode=episode WHERE job_id=?",
                    (row["job_id"],),
                )
                result.append(notice)
        return tuple(result)
