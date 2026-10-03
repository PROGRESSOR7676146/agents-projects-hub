"""Passive accepted-turn activity; never changes execution or approval authority."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone

from .codex_activity import CodexActivityEvent
from .task_activity_binding import ACTIVITY_BINDING as _BINDING
from .task_activity_binding import current_activity_binding
from .task_lifecycle import TaskLifecycleNotice, TaskLifecycleState

MAX_ACTIVITY_ENTRIES = 512
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", re.ASCII)
_TOOLS = {
    "command",
    "file_change",
    "mcp",
    "dynamic_tool",
    "collaboration",
    "web_search",
    "image_view",
}


class TaskActivityState:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
        state_error: Callable[[str], Exception],
        notices: TaskLifecycleState,
        notices_enabled: bool = False,
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.state_error = state_error
        self.notices = notices
        self.notices_enabled = notices_enabled

    def _time(self, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise self.state_error("activity timestamp must be timezone-aware")
        return now.astimezone(timezone.utc).isoformat()

    def _transaction_required(self) -> None:
        if not self.db.in_transaction:
            raise self.state_error("activity update requires an active transaction")

    def _current(self, job_id: str, token: str, timestamp: str) -> sqlite3.Row | None:
        return current_activity_binding(self.db, job_id, token, timestamp)

    def _live(self, job_id: str, token: str, timestamp: str) -> sqlite3.Row:
        row = self._current(job_id, token, timestamp)
        if row is None:
            raise self.state_error("activity requires the exact current accepted execution")
        return row

    def _bound(self, job_id: str, token: str, timestamp: str) -> sqlite3.Row:
        live = self._live(job_id, token, timestamp)
        row = self.db.execute("SELECT * FROM task_activity WHERE job_id=?", (job_id,)).fetchone()
        if row is None or any(row[key] != live[key] for key in _BINDING):
            raise self.state_error("activity binding changed or is missing")
        return row

    def bind_accepted(
        self,
        job_id: str,
        token: str,
        thread_id: str,
        turn_id: str,
        project_root: str,
        *,
        now: datetime,
    ) -> bool:
        with self.transaction():
            return self.bind_accepted_in_transaction(
                job_id, token, thread_id, turn_id, project_root, now=now
            )

    def bind_accepted_in_transaction(
        self,
        job_id: str,
        token: str,
        thread_id: str,
        turn_id: str,
        project_root: str,
        *,
        now: datetime,
    ) -> bool:
        self._transaction_required()
        timestamp = self._time(now)
        live = self._live(job_id, token, timestamp)
        if (live["provider_thread_id"], live["provider_turn_id"], live["project_root"]) != (
            thread_id,
            turn_id,
            project_root,
        ):
            raise self.state_error("activity checkpoint identity does not match")
        prior = self.db.execute("SELECT * FROM task_activity WHERE job_id=?", (job_id,)).fetchone()
        if prior is not None:
            self._bound(job_id, token, timestamp)
            return False
        self.db.execute(
            "INSERT INTO task_activity (job_id," + ",".join(_BINDING) + ",mode,last_meaningful_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ordinary',?)",
            (job_id, *(live[key] for key in _BINDING), timestamp),
        )
        return True

    @staticmethod
    def _digest(value: str | int) -> str:
        return hashlib.sha256((type(value).__name__ + ":" + str(value)).encode()).hexdigest()

    def _event_identity(self, event: CodexActivityEvent) -> tuple[str, str, str | None] | None:
        if not isinstance(event, CodexActivityEvent):
            raise self.state_error("invalid activity observation")
        if event.kind == "retrying" and event.category == "retry":
            return None
        if not isinstance(event.item_id, str) or not _IDENTITY.fullmatch(event.item_id):
            raise self.state_error("invalid activity item identity")
        item = self._digest(event.item_id)
        if (
            event.kind in {"tool_started", "tool_completed", "tool_output"}
            and event.category in _TOOLS
        ):
            if event.kind == "tool_output" and event.category != "command":
                raise self.state_error("invalid activity output category")
            return "tool", item, None
        if event.kind == "visible_message_completed" and event.category == "visible_message":
            if event.phase not in {None, "commentary", "final_answer", "unknown"}:
                raise self.state_error("invalid visible activity phase")
            return "message", item, None
        if event.kind in {"approval_requested", "approval_resolved"} and event.category in {
            "command",
            "file_change",
            "network",
            "permissions",
        }:
            request = event.request_id
            if not (
                (
                    isinstance(request, int)
                    and not isinstance(request, bool)
                    and -(2**63) <= request < 2**63
                )
                or (isinstance(request, str) and _IDENTITY.fullmatch(request))
            ):
                raise self.state_error("invalid approval request identity")
            return "approval", self._digest(request), item
        raise self.state_error("invalid activity kind or category")

    def record_activity(
        self, job_id: str, token: str, event: CodexActivityEvent, *, now: datetime
    ) -> bool:
        with self.transaction():
            return self.record_activity_in_transaction(job_id, token, event, now=now)

    def record_activity_in_transaction(
        self, job_id: str, token: str, event: CodexActivityEvent, *, now: datetime
    ) -> bool:
        self._transaction_required()
        timestamp = self._time(now)
        bound = self._bound(job_id, token, timestamp)
        identity = self._event_identity(event)
        if (event.thread_id, event.turn_id) != (
            bound["provider_thread_id"],
            bound["provider_turn_id"],
        ):
            raise self.state_error("activity event belongs to another accepted turn")
        if identity is None or timestamp < bound["last_meaningful_at"]:
            return False
        kind, key, item = identity
        prior = self.db.execute(
            "SELECT * FROM task_activity_entries WHERE job_id=? AND kind=? AND identity=?",
            (job_id, kind, key),
        ).fetchone()
        if prior is not None and (prior["category"], prior["item_identity"]) != (
            event.category,
            item,
        ):
            raise self.state_error("activity identity metadata changed")
        state = {
            "tool_started": "active",
            "tool_completed": "completed",
            "tool_output": "active",
            "visible_message_completed": "completed",
            "approval_requested": "pending",
            "approval_resolved": "resolved",
        }[event.kind]
        if event.kind in {"tool_output", "approval_resolved"} and prior is None:
            return False
        if prior is not None:
            if prior["state"] in {"completed", "resolved"}:
                return False
            if prior["state"] == state and event.kind != "tool_output":
                return False
            # Output callbacks have no provider event ID: each scoped observation
            # advances progress, without storing raw output or claiming replay dedup.
            self.db.execute(
                "UPDATE task_activity_entries SET state=? WHERE job_id=? AND kind=? AND identity=?",
                (state, job_id, kind, key),
            )
        else:
            count = self.db.execute(
                "SELECT count(*) FROM task_activity_entries WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            if count >= MAX_ACTIVITY_ENTRIES:
                return False
            self.db.execute(
                "INSERT INTO task_activity_entries VALUES(?,?,?,?,?,?)",
                (job_id, kind, key, event.category, item, state),
            )
        pending = self.db.execute(
            "SELECT 1 FROM task_activity_entries WHERE job_id=? AND kind='approval' AND state='pending'",
            (job_id,),
        ).fetchone()
        active = self.db.execute(
            "SELECT 1 FROM task_activity_entries WHERE job_id=? AND kind='tool' AND state='active'",
            (job_id,),
        ).fetchone()
        mode = "approval" if pending else "tool" if active else "ordinary"
        self.db.execute(
            "UPDATE task_activity SET mode=?,last_meaningful_at=?,episode=episode+1,notified_episode=NULL WHERE job_id=?",
            (mode, timestamp, job_id),
        )
        self._supersede(job_id, "no_progress", timestamp)
        if event.kind == "approval_resolved":
            self._supersede(
                job_id, "approval_wait", timestamp, event_key=f"activity:{job_id}:approval:{key}"
            )
        elif event.kind == "approval_requested" and self.notices_enabled:
            self._notice(
                bound,
                event_key=f"activity:{job_id}:approval:{key}",
                kind="approval_wait",
                text="Codex is waiting for human permission for "
                + {
                    "command": "command execution",
                    "file_change": "file changes",
                    "network": "network access",
                    "permissions": "additional permissions",
                }[event.category]
                + ". Open the exact request in Codex/tlive to review and allow or deny it, "
                "or use /stop in this topic. Hub cannot approve it.",
                now=now,
            )
        return True

    def _supersede(
        self, job_id: str, kind: str, timestamp: str, *, event_key: str | None = None
    ) -> None:
        sql = "UPDATE task_lifecycle_notices SET status='superseded',updated_at=? WHERE job_id=? AND kind=? AND status IN ('pending','leased') AND attempt_count=0 AND send_started_at IS NULL"
        args: tuple[object, ...] = (timestamp, job_id, kind)
        if event_key is not None:
            sql += " AND event_key=?"
            args += (event_key,)
        self.db.execute(sql, args)

    def _notice(
        self, bound: sqlite3.Row, *, event_key: str, kind: str, text: str, now: datetime
    ) -> TaskLifecycleNotice:
        notice, _ = self.notices.prepare_notice_in_transaction(
            event_key=event_key,
            kind=kind,
            job_id=bound["job_id"],
            chat_id=bound["chat_id"],
            thread_id=bound["thread_id"],
            telegram_html=text,
            now=now,
        )
        return notice

    def evaluate(
        self, *, now: datetime, ordinary_seconds: int = 300, tool_seconds: int = 1200
    ) -> tuple[TaskLifecycleNotice, ...]:
        with self.transaction():
            return self.evaluate_in_transaction(
                now=now, ordinary_seconds=ordinary_seconds, tool_seconds=tool_seconds
            )

    def evaluate_in_transaction(
        self, *, now: datetime, ordinary_seconds: int = 300, tool_seconds: int = 1200
    ) -> tuple[TaskLifecycleNotice, ...]:
        self._transaction_required()
        timestamp = self._time(now)
        for seconds in (ordinary_seconds, tool_seconds):
            if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
                raise self.state_error("activity threshold must be a positive integer")
        if not self.notices_enabled:
            return ()
        result = []
        rows = self.db.execute(
            "SELECT activity.* FROM task_activity activity JOIN provider_jobs job "
            "ON job.job_id=activity.job_id AND job.status='executing' "
            "AND job.lease_token=activity.lease_token AND job.lease_expires_at>? "
            "WHERE activity.mode!='approval' AND "
            "(activity.notified_episode IS NULL OR activity.notified_episode!=activity.episode) "
            "ORDER BY activity.last_meaningful_at,activity.job_id LIMIT 256",
            (timestamp,),
        ).fetchall()
        for row in rows:
            # Recheck current execution identity without turning stale observations
            # into execution failures or mutating their writer/root ownership.
            live = self._current(row["job_id"], row["lease_token"], timestamp)
            if live is None or any(row[key] != live[key] for key in _BINDING):
                continue
            bound = row
            elapsed = (now - datetime.fromisoformat(bound["last_meaningful_at"])).total_seconds()
            if elapsed < (tool_seconds if bound["mode"] == "tool" else ordinary_seconds):
                continue
            result.append(
                self._notice(
                    bound,
                    event_key=f"activity:{row['job_id']}:no-progress:{row['episode']}",
                    kind="no_progress",
                    text="No new progress has been observed from the active "
                    + ("tool or build" if bound["mode"] == "tool" else "turn")
                    + ". The task may still be running. Check /status or use /stop in this topic; work has not been restarted.",
                    now=now,
                )
            )
            self.db.execute(
                "UPDATE task_activity SET notified_episode=episode WHERE job_id=?", (row["job_id"],)
            )
        return tuple(result)
