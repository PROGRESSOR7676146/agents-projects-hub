from __future__ import annotations

import sqlite3
from pathlib import PurePath

from .delivery_control_predicates import (
    legacy_hold_has_full_control,
    result_ready_control_reconciled,
)
from .state_errors import StateError
from .state_values import _bounded, _now


class LaneState:
    """Lane queries and policy on the connection owned by HubState."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def _require_transaction(self) -> None:
        if not self._connection.in_transaction:
            raise StateError("lane mutation requires a caller-owned transaction")

    def register_in_transaction(
        self,
        *,
        lane_id: str,
        project_id: str,
        worktree_path: str,
        branch_name: str,
        now: str,
        topic_id: int | None = None,
    ) -> None:
        self._require_transaction()
        resolved_path = worktree_path
        self._connection.execute(
            """INSERT INTO worktree_lanes
               (lane_id, project_id, topic_id, worktree_path, branch_name,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                lane_id,
                project_id,
                None,
                resolved_path,
                branch_name,
                now,
                now,
            ),
        )
        if topic_id is not None:
            self._bind_lane_locked(lane_id, topic_id, now=now)

    def archive_in_transaction(
        self,
        lane_id: str,
        *,
        project_id: str | None = None,
        project_root: PurePath | None = None,
    ) -> None:
        self._require_transaction()
        lane = self._connection.execute(
            "SELECT * FROM worktree_lanes WHERE lane_id = ? AND status = 'active'",
            (lane_id,),
        ).fetchone()
        if lane is None:
            raise StateError(f"unknown or inactive lane_id: {lane_id}")
        topic_id = lane["topic_id"]
        destination_scope: str | None = None
        if topic_id is not None:
            self._require_topic_execution_idle_locked(int(topic_id))
            topic = self._connection.execute(
                "SELECT * FROM topics WHERE topic_id=?", (int(topic_id),)
            ).fetchone()
            if topic is None:
                raise StateError(f"unknown topic_id: {topic_id}")
            if (
                project_id != lane["project_id"]
                or project_id != topic["project_id"]
                or project_root is None
                or not project_root.is_absolute()
            ):
                raise StateError("bound lane archive requires its validated project root")
            if topic["execution_scope"] != f"root:{lane['worktree_path']}":
                raise StateError("active lane execution scope mismatch")
            destination_scope = "root:" + _bounded(
                str(project_root), name="execution root", maximum=4096
            )
            self._require_execution_scope_idle_locked(f"root:{lane['worktree_path']}")
            self._require_execution_scope_idle_locked(destination_scope, binding_change=False)
            legacy_topics = self._connection.execute(
                """SELECT topic_id FROM topics WHERE execution_scope IS NULL
                   OR execution_scope = '' OR execution_scope = 'project:' || project_id"""
            ).fetchall()
            for legacy in legacy_topics:
                self._require_topic_execution_idle_locked(
                    int(legacy["topic_id"]), binding_change=False
                )
        cursor = self._connection.execute(
            "UPDATE worktree_lanes SET status = 'archived', updated_at = ? WHERE lane_id = ?",
            (_now(), lane_id),
        )
        if cursor.rowcount != 1:
            raise StateError(f"unknown lane_id: {lane_id}")
        if destination_scope is not None:
            self._connection.execute(
                """UPDATE topics SET execution_scope = ?, updated_at = ?
                   WHERE topic_id = ?""",
                (destination_scope, _now(), topic_id),
            )

    def mark_cleaned(self, lane_id: str) -> int:
        cursor = self._connection.execute(
            """UPDATE worktree_lanes SET cleaned_at = ?, updated_at = ?
               WHERE lane_id = ? AND status = 'archived' AND cleaned_at IS NULL""",
            (_now(), _now(), lane_id),
        )
        return cursor.rowcount

    def bind_in_transaction(self, lane_id: str, topic_id: int, *, now: str) -> None:
        self._require_transaction()
        self._bind_lane_locked(lane_id, topic_id, now=now)

    def _bind_lane_locked(self, lane_id: str, topic_id: int, *, now: str) -> None:
        lane = self._connection.execute(
            "SELECT * FROM worktree_lanes WHERE lane_id = ?", (lane_id,)
        ).fetchone()
        if lane is None or lane["status"] != "active":
            raise StateError(f"unknown or inactive lane_id: {lane_id}")
        if lane["topic_id"] is not None:
            raise StateError("active lane is already bound")
        topic = self._connection.execute(
            "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if topic is None:
            raise StateError(f"unknown topic_id: {topic_id}")
        if lane["project_id"] != topic["project_id"]:
            raise StateError("lane and Telegram topic belong to different projects")
        conflict = self._connection.execute(
            """SELECT lane_id FROM worktree_lanes
               WHERE topic_id = ? AND lane_id != ? AND status = 'active'""",
            (topic_id, lane_id),
        ).fetchone()
        if conflict is not None:
            raise StateError("Telegram topic is already bound to another active lane")
        self._require_topic_execution_idle_locked(topic_id)
        self._require_execution_scope_idle_locked(f"root:{lane['worktree_path']}")
        self._connection.execute(
            "UPDATE worktree_lanes SET topic_id = ?, updated_at = ? WHERE lane_id = ?",
            (topic_id, now, lane_id),
        )
        self._connection.execute(
            "UPDATE topics SET execution_scope = ?, updated_at = ? WHERE topic_id = ?",
            (f"root:{lane['worktree_path']}", now, topic_id),
        )

    def _require_topic_execution_idle_locked(
        self, topic_id: int, *, binding_change: bool = True
    ) -> None:
        """Peer checks retain busy ownership; only relocation checks retained bindings."""
        if binding_change and (
            self._connection.execute(
                f"SELECT 1 FROM telegram_delivery_hold_dispositions hold WHERE topic_id=? "
                f"AND NOT {legacy_hold_has_full_control('hold')} LIMIT 1",
                (topic_id,),
            ).fetchone()
            is not None
        ):
            raise StateError("delivery hold disposition retains the topic binding")
        job = self._connection.execute(
            f"""SELECT 1 FROM provider_jobs jobs
               WHERE jobs.topic_id = ? AND (
                 (jobs.status IN ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
                  AND NOT {result_ready_control_reconciled("jobs")})
                 OR (jobs.status = 'indeterminate' AND NOT EXISTS (
                   SELECT 1 FROM provider_job_resolutions resolutions
                   WHERE resolutions.job_id = jobs.job_id
                 ) AND NOT EXISTS (
                   SELECT 1 FROM provider_turn_terminal_evidence evidence
                   WHERE evidence.job_id = jobs.job_id
                 ))
               ) LIMIT 1""",
            (topic_id,),
        ).fetchone()
        dispatch = self._connection.execute(
            """SELECT 1 FROM turn_dispatches
               WHERE topic_id = ? AND status IN ('queued', 'running') LIMIT 1""",
            (topic_id,),
        ).fetchone()
        writer = self._connection.execute(
            """SELECT 1 FROM agent_sessions
               WHERE topic_id = ? AND status IN ('active', 'satellite')
                 AND writer_mode != 'telegram' LIMIT 1""",
            (topic_id,),
        ).fetchone()
        bound_session = self._connection.execute(
            """SELECT 1 FROM agent_sessions
               WHERE topic_id = ? AND status IN ('active', 'satellite')
                 AND provider_session_id IS NOT NULL AND ? LIMIT 1""",
            (topic_id, binding_change),
        ).fetchone()
        if any(item is not None for item in (job, dispatch, writer, bound_session)):
            raise StateError("Telegram topic has active or unresolved execution")

    def _require_execution_scope_idle_locked(
        self, execution_scope: str, *, binding_change: bool = True
    ) -> None:
        job = self._connection.execute(
            f"""SELECT 1 FROM provider_jobs jobs
               JOIN topics ON topics.topic_id = jobs.topic_id
               WHERE COALESCE(topics.execution_scope, 'project:' || topics.project_id) = ?
                 AND (
                   (jobs.status IN ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
                    AND NOT {result_ready_control_reconciled("jobs")})
                   OR (jobs.status = 'indeterminate' AND NOT EXISTS (
                     SELECT 1 FROM provider_job_resolutions resolutions
                     WHERE resolutions.job_id = jobs.job_id
                   ) AND NOT EXISTS (
                     SELECT 1 FROM provider_turn_terminal_evidence evidence
                     WHERE evidence.job_id = jobs.job_id
                   ))
                 ) LIMIT 1""",
            (execution_scope,),
        ).fetchone()
        dispatch = self._connection.execute(
            """SELECT 1 FROM turn_dispatches dispatches
               JOIN topics ON topics.topic_id = dispatches.topic_id
               WHERE COALESCE(topics.execution_scope, 'project:' || topics.project_id) = ?
                 AND dispatches.status IN ('queued', 'running') LIMIT 1""",
            (execution_scope,),
        ).fetchone()
        writer = self._connection.execute(
            """SELECT 1 FROM agent_sessions sessions
               JOIN topics ON topics.topic_id = sessions.topic_id
               WHERE COALESCE(topics.execution_scope, 'project:' || topics.project_id) = ?
                 AND sessions.status IN ('active', 'satellite')
                 AND sessions.writer_mode != 'telegram' LIMIT 1""",
            (execution_scope,),
        ).fetchone()
        bound_session = self._connection.execute(
            """SELECT 1 FROM agent_sessions sessions
               JOIN topics ON topics.topic_id = sessions.topic_id
               WHERE COALESCE(topics.execution_scope, 'project:' || topics.project_id) = ?
                 AND sessions.status IN ('active', 'satellite')
                 AND sessions.provider_session_id IS NOT NULL AND ? LIMIT 1""",
            (execution_scope, binding_change),
        ).fetchone()
        if any(item is not None for item in (job, dispatch, writer, bound_session)):
            raise StateError("execution scope has active or unresolved execution")

    def active_lane_for_topic(self, topic_id: int) -> dict[str, object] | None:
        row = self._connection.execute(
            """SELECT * FROM worktree_lanes
               WHERE topic_id = ? AND status = 'active'""",
            (topic_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def get_lane(self, lane_id: str) -> dict[str, object]:
        row = self._connection.execute(
            "SELECT * FROM worktree_lanes WHERE lane_id = ?", (lane_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown lane_id: {lane_id}")
        return dict(row)

    def list_lanes(self) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM worktree_lanes ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]
