"""Bounded queue snapshots and handoff notices in caller-owned transactions.

Admission opts a new job in by persisting its accepted notice. Batched inputs
never create another acceptance or opt an existing job in. Queue categories are
reported at most once per job, not as re-arming episodes. Capacity and worker
availability remain unknown without exact scheduler inputs; this facade does
not infer them from missing health or assume a single provider slot.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .provider_queue_capacity import QueueCapacityConfig, read_queue_capacity
from .task_lifecycle import TaskLifecycleState


@dataclass(frozen=True, slots=True)
class QueueWaitSnapshot:
    reason: str
    explanation: str
    owner_chat_id: int | None = None
    owner_thread_id: int | None = None


class QueueVisibilityState:
    """Prepare delivery-only notices; never claim a lease or commit a transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        notices: TaskLifecycleState,
        *,
        state_error: Callable[[str], Exception],
    ) -> None:
        self.db = connection
        self.notices = notices
        self.state_error = state_error

    def _require_transaction(self) -> None:
        if not self.db.in_transaction:
            raise self.state_error("queue visibility requires an active transaction")

    def _timestamp(self, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise self.state_error("queue visibility timestamp must be timezone-aware")
        return now.astimezone(timezone.utc).isoformat()

    def _job(self, job_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT job.*, topic.thread_id, "
            "COALESCE(topic.execution_scope, 'project:' || topic.project_id) AS scope "
            "FROM provider_jobs job JOIN topics topic ON topic.topic_id=job.topic_id "
            "WHERE job.job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise self.state_error("queue visibility job does not exist")
        return row

    @property
    def available(self) -> bool:
        # Older-schema migration fixtures and rollback readers must not acquire
        # a notice dependency when their jobs never opted into this feature.
        return (
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_lifecycle_notices'"
            ).fetchone()
            is not None
        )

    def enabled(self, job_id: str) -> bool:
        if not self.available:
            return False
        return (
            self.db.execute(
                "SELECT 1 FROM task_lifecycle_notices WHERE event_key=? "
                "AND kind='accepted' AND job_id=?",
                (f"job:{job_id}:accepted", job_id),
            ).fetchone()
            is not None
        )

    def _prepare(self, job: sqlite3.Row, *, key: str, kind: str, text: str, now: datetime) -> None:
        # A category is a historical snapshot, including its first owner topic.
        # Later observations must not replace its immutable text or repeat it.
        if (
            self.db.execute(
                "SELECT 1 FROM task_lifecycle_notices WHERE event_key=?", (key,)
            ).fetchone()
            is not None
        ):
            return
        self.notices.prepare_notice_in_transaction(
            event_key=key,
            kind=kind,
            job_id=str(job["job_id"]),
            chat_id=int(job["chat_id"]),
            thread_id=int(job["thread_id"]),
            reply_to_message_id=int(job["message_id"]),
            telegram_html=text,
            now=now,
        )

    def admitted_in_transaction(
        self, job_id: str, *, now: datetime, capacity: QueueCapacityConfig | None = None
    ) -> None:
        self._require_transaction()
        job = self._job(job_id)
        if job["status"] != "queued":
            raise self.state_error("queue visibility admission requires a new queued job")
        self._prepare(
            job,
            key=f"job:{job_id}:accepted",
            kind="accepted",
            text="Accepted by Hub and saved in the queue. Provider work has not started.",
            now=now,
        )
        self.queued_in_transaction(job_id, now=now, capacity=capacity)

    def queued_in_transaction(
        self, job_id: str, *, now: datetime, capacity: QueueCapacityConfig | None = None
    ) -> None:
        self._require_transaction()
        if not self.enabled(job_id):
            return
        job = self._job(job_id)
        if job["status"] not in {"queued", "retry_wait"}:
            return
        snapshot = self.wait_snapshot(job_id, now=now, capacity=capacity)
        owner = ""
        if snapshot.owner_chat_id is not None and snapshot.owner_thread_id is not None:
            chat = str(snapshot.owner_chat_id)
            if chat.startswith("-100") and chat[4:].isdigit() and snapshot.owner_thread_id > 0:
                owner = (
                    f' Owner: <a href="https://t.me/c/{chat[4:]}/{snapshot.owner_thread_id}">'
                    "owning topic</a>."
                )
        self._prepare(
            job,
            key=f"job:{job_id}:queued:{snapshot.reason}",
            kind="queued",
            text="Queued; provider work has not started. " + snapshot.explanation + owner,
            now=now,
        )

    def wait_snapshot(
        self, job_id: str, *, now: datetime, capacity: QueueCapacityConfig | None = None
    ) -> QueueWaitSnapshot:
        """Read the actual FIFO/root/deadline blockers, without changing scheduling."""
        self._require_transaction()
        job = self._job(job_id)
        timestamp = self._timestamp(now)
        if (
            self.db.execute(
                "SELECT 1 FROM provider_job_holds WHERE job_id=? AND decision='pending'",
                (job_id,),
            ).fetchone()
            is not None
        ):
            return QueueWaitSnapshot("held", "Owner review is required before this job can run.")
        continuation = (
            self.db.execute(
                "SELECT 1 FROM provider_job_continuations WHERE continuation_job_id=?", (job_id,)
            ).fetchone()
            is not None
        )
        if not continuation:
            earlier = self.db.execute(
                "SELECT 1 FROM provider_jobs WHERE topic_id=? AND topic_sequence<? "
                "AND status NOT IN ('completed','failed','cancelled','indeterminate') LIMIT 1",
                (job["topic_id"], job["topic_sequence"]),
            ).fetchone()
            if earlier is not None:
                return QueueWaitSnapshot(
                    "topic_fifo",
                    "An earlier job in this topic must finish, including result delivery. "
                    "Wait or use /stop in this topic to cancel waiting work.",
                )
            held = self.db.execute(
                "SELECT topic.chat_id, topic.thread_id FROM provider_jobs earlier "
                "JOIN topics topic ON topic.topic_id=earlier.topic_id "
                "JOIN provider_job_holds held ON held.job_id=earlier.job_id "
                "WHERE held.decision='pending' AND earlier.status IN ('queued','retry_wait') "
                "AND COALESCE(topic.execution_scope,'project:' || topic.project_id)=? "
                "AND (earlier.created_at<? OR (earlier.created_at=? AND earlier.job_id<?)) "
                "ORDER BY earlier.created_at,earlier.job_id LIMIT 1",
                (job["scope"], job["created_at"], job["created_at"], job_id),
            ).fetchone()
            if held is not None:
                return QueueWaitSnapshot(
                    "root_held_fifo",
                    "An earlier request on this project awaits owner review.",
                    int(held["chat_id"]),
                    int(held["thread_id"]),
                )
        active = self.db.execute(
            "SELECT other.status, topic.chat_id, topic.thread_id FROM provider_jobs other "
            "JOIN topics topic ON topic.topic_id=other.topic_id "
            "WHERE other.job_id!=? "
            "AND COALESCE(topic.execution_scope,'project:' || topic.project_id)=? "
            "AND (other.status='executing' OR (other.status='leased' AND other.lease_expires_at>?) "
            "OR (other.status='indeterminate' "
            "AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions r WHERE r.job_id=other.job_id) "
            "AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence e "
            "WHERE e.job_id=other.job_id))) ORDER BY other.created_at,other.job_id LIMIT 1",
            (job_id, job["scope"], timestamp),
        ).fetchone()
        if active is not None:
            uncertain = active["status"] == "indeterminate"
            return QueueWaitSnapshot(
                "root_uncertain" if uncertain else "root_active",
                "An earlier outcome on this project is unknown. Reconcile it in the owning topic; "
                "a timeout does not release the project."
                if uncertain
                else "Other active work owns this project. Wait or use /stop in the owning "
                "topic; an interrupt request alone does not release the project.",
                int(active["chat_id"]),
                int(active["thread_id"]),
            )
        writer = self.db.execute(
            "SELECT session.writer_mode, topic.chat_id, topic.thread_id FROM agent_sessions session "
            "JOIN topics topic ON topic.topic_id=session.topic_id "
            "WHERE session.status IN ('active','satellite') AND session.writer_mode!='telegram' "
            "AND COALESCE(topic.execution_scope,'project:' || topic.project_id)=? "
            "ORDER BY session.updated_at,session.session_id LIMIT 1",
            (job["scope"],),
        ).fetchone()
        if writer is not None:
            command = "/return" if writer["writer_mode"] == "local" else "/release"
            return QueueWaitSnapshot(
                "root_writer",
                f"A local writer owns the project. Close that client and use {command} "
                "in the owning topic, then review held work.",
                int(writer["chat_id"]),
                int(writer["thread_id"]),
            )
        dispatch = self.db.execute(
            "SELECT topic.chat_id,topic.thread_id FROM turn_dispatches dispatch "
            "JOIN topics topic ON topic.topic_id=dispatch.topic_id WHERE dispatch.status='running' "
            "AND COALESCE(topic.execution_scope,'project:' || topic.project_id)=? "
            "ORDER BY dispatch.created_at LIMIT 1",
            (job["scope"],),
        ).fetchone()
        if dispatch is not None:
            return QueueWaitSnapshot(
                "root_dispatch",
                "A running dispatch owns this project. Wait for it to finish.",
                int(dispatch["chat_id"]),
                int(dispatch["thread_id"]),
            )
        if job["next_attempt_at"] is not None and job["next_attempt_at"] > timestamp:
            if job["status"] == "retry_wait":
                return QueueWaitSnapshot(
                    "retry_deadline",
                    "The saved pre-execution retry deadline has not arrived. Wait; "
                    "this does not repeat an accepted provider turn.",
                )
            return QueueWaitSnapshot(
                "collecting",
                "Hub is collecting compatible messages until the saved input deadline. "
                "Wait; no provider capacity has been inferred.",
            )
        if capacity is not None:
            slots = read_queue_capacity(self.db, capacity, now=now)
            if slots.occupied_roots >= slots.effective_capacity:
                return QueueWaitSnapshot(
                    "global_capacity",
                    f"All {slots.effective_capacity} configured global project slots are occupied. "
                    "Wait for active work to finish, or use /stop here to cancel waiting work.",
                )
            provider_limit = capacity.agent_capacities.get(str(job["agent_id"]), 1)
            if slots.busy_agents.get(str(job["agent_id"]), 0) >= provider_limit:
                return QueueWaitSnapshot(
                    "provider_slots",
                    f"All {provider_limit} configured slots for this provider are occupied. "
                    "Wait for active work to finish, or use /stop here to cancel waiting work.",
                )
            return QueueWaitSnapshot(
                "worker_unknown",
                "Awaiting a worker claim. Configured capacity is not blocking this snapshot; "
                "worker availability remains unknown. Wait or use /stop here to cancel waiting work.",
            )
        return QueueWaitSnapshot(
            "worker_unknown",
            "Awaiting a worker claim. Provider slot availability and global "
            "capacity are unknown here. Wait or use /stop in this topic to cancel waiting work.",
        )

    def sync_job_in_transaction(self, job_id: str, *, now: datetime) -> None:
        """Supersede stale snapshots only if no network send has ever begun."""
        self._require_transaction()
        if not self.enabled(job_id):
            return
        status = self._job(job_id)["status"]
        kinds = (
            ("accepted", "queued")
            if status == "executing"
            else (
                ("executing",)
                if status in {"queued", "retry_wait"}
                else (() if status == "leased" else ("accepted", "queued", "executing"))
            )
        )
        if not kinds:
            return
        placeholders = ",".join("?" for _ in kinds)
        self.db.execute(
            f"UPDATE task_lifecycle_notices SET status='superseded',lease_token=NULL,"
            f"lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=? "
            f"AND kind IN ({placeholders}) AND status IN ('pending','leased') "
            "AND send_started_at IS NULL AND attempt_count=0",
            (self._timestamp(now), job_id, *kinds),
        )

    def executing_in_transaction(self, job_id: str, *, now: datetime) -> None:
        self._require_transaction()
        self.sync_job_in_transaction(job_id, now=now)
        if not self.enabled(job_id):
            return
        job = self._job(job_id)
        if job["status"] != "executing":
            return
        self._prepare(
            job,
            key=f"job:{job_id}:executing:{job['attempt_count']}",
            kind="executing",
            text="Worker started the provider handoff. Native turn acceptance is not yet confirmed. "
            "Use /stop in this topic to request interruption; that request is not terminal proof.",
            now=now,
        )
