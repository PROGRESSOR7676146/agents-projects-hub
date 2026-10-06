from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from .provider_queue_capacity import (
    PROVIDER_WORKER_FAIRNESS_FRESHNESS,
    QueueCapacityConfig,
    read_queue_capacity,
)
from .provider_queue_capacity import (
    parallel_worker_declarations as _parallel_worker_declarations,
)
from .queue_visibility import QueueVisibilityState
from .stop_coverage import STOP_COVERS_JOB_SQL, pending_stop_for_job, stop_covers

# A stop is complete once none of its covered work can still start or run: a
# follow-up that is leased, or that a rejected steering call returned to the
# queue, keeps the stop pending even after the parent turn was cancelled. The
# rule is applied to every pending stop of the topic at each cancellation, so
# work cancelled by a later stop also completes the earlier one.
COMPLETE_FINISHED_STOPS_SQL = f"""UPDATE provider_stop_requests
   SET status = 'completed', completed_at = ?
   WHERE request_id IN (
     SELECT stop.request_id FROM provider_stop_requests stop
     WHERE stop.status = 'pending' AND stop.topic_id = ?
       AND NOT EXISTS (
         SELECT 1 FROM provider_jobs other
         WHERE (other.status IN ('queued', 'retry_wait', 'leased', 'executing')
           OR (other.status = 'indeterminate'
             AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence e
                             WHERE e.job_id = other.job_id)
             AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions r
                             WHERE r.job_id = other.job_id)))
           AND {stop_covers("stop", "other")}
       )
   )"""


_ELIGIBLE_PROVIDER_JOB_SQL = """SELECT candidate.* FROM provider_jobs candidate
   JOIN topics candidate_topic ON candidate_topic.topic_id = candidate.topic_id
   WHERE candidate.agent_id = ?
     AND candidate.attempt_count < candidate.max_attempts
     AND (
       (candidate.status = 'queued'
           AND (candidate.next_attempt_at IS NULL OR candidate.next_attempt_at <= ?))
       OR (candidate.status = 'retry_wait'
           AND candidate.next_attempt_at IS NOT NULL AND candidate.next_attempt_at <= ?)
     )
     AND NOT EXISTS (
       SELECT 1 FROM provider_job_holds held WHERE held.job_id = candidate.job_id
         AND held.decision = 'pending'
     )
     AND (EXISTS (SELECT 1 FROM provider_job_continuations special
                  WHERE special.continuation_job_id = candidate.job_id)
          OR NOT EXISTS (
       SELECT 1 FROM provider_jobs earlier
       WHERE earlier.topic_id = candidate.topic_id
         AND earlier.topic_sequence < candidate.topic_sequence
         AND earlier.status NOT IN ('completed', 'failed', 'cancelled', 'indeterminate')
     ))
     AND (EXISTS (SELECT 1 FROM provider_job_continuations special
                  WHERE special.continuation_job_id = candidate.job_id)
          OR NOT EXISTS (
       SELECT 1 FROM provider_jobs earlier
       JOIN topics earlier_topic ON earlier_topic.topic_id=earlier.topic_id
       WHERE COALESCE(earlier_topic.execution_scope,
                      'project:' || earlier_topic.project_id)=
             COALESCE(candidate_topic.execution_scope,
                      'project:' || candidate_topic.project_id)
         AND (earlier.created_at < candidate.created_at
              OR (earlier.created_at=candidate.created_at
                  AND earlier.job_id<candidate.job_id))
         AND earlier.status IN ('queued','retry_wait')
         AND EXISTS (SELECT 1 FROM provider_job_holds held
                     WHERE held.job_id=earlier.job_id AND held.decision='pending')
     ))
     AND NOT EXISTS (
       SELECT 1 FROM provider_jobs active
       JOIN topics active_topic ON active_topic.topic_id = active.topic_id
       WHERE active.job_id != candidate.job_id
         AND COALESCE(active_topic.execution_scope, 'project:' || active_topic.project_id) =
             COALESCE(candidate_topic.execution_scope, 'project:' || candidate_topic.project_id)
         AND (
           active.status = 'executing'
           OR (active.status = 'leased' AND active.lease_expires_at > ?)
           OR (active.status = 'indeterminate' AND NOT EXISTS (
             SELECT 1 FROM provider_job_resolutions resolutions
             WHERE resolutions.job_id = active.job_id
           ) AND NOT EXISTS (
             SELECT 1 FROM provider_turn_terminal_evidence evidence
             WHERE evidence.job_id = active.job_id
           ))
         )
     )
     AND NOT EXISTS (
       SELECT 1 FROM agent_sessions writer
       JOIN topics writer_topic ON writer_topic.topic_id = writer.topic_id
       WHERE writer.status IN ('active', 'satellite')
         AND writer.writer_mode != 'telegram'
         AND COALESCE(writer_topic.execution_scope, 'project:' || writer_topic.project_id) =
             COALESCE(candidate_topic.execution_scope, 'project:' || candidate_topic.project_id)
     )
     AND NOT EXISTS (
       SELECT 1 FROM turn_dispatches dispatch
       JOIN topics dispatch_topic ON dispatch_topic.topic_id = dispatch.topic_id
       WHERE dispatch.status = 'running'
         AND COALESCE(dispatch_topic.execution_scope, 'project:' || dispatch_topic.project_id) =
             COALESCE(candidate_topic.execution_scope, 'project:' || candidate_topic.project_id)
     )
   ORDER BY candidate.created_at, candidate.topic_id, candidate.topic_sequence
   LIMIT 1"""


@dataclass(frozen=True, slots=True)
class ProviderJobRecord:
    job_id: str
    idempotency_key: str
    chat_id: int
    message_id: int
    topic_id: int
    topic_sequence: int
    agent_id: str
    session_id: str
    session_generation: int
    provider_session_id: str | None
    model: str
    effort: str
    payload_text: str
    context_watermark: int | None
    handoff_id: str | None
    input_group_key: str | None
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: str | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None
    provider_started_at: str | None
    error_class: str | None
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str
    codex_permission_profile: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderJobRecovery:
    requeued_job_ids: tuple[str, ...]
    indeterminate_job_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderChatActivity:
    agent_id: str
    chat_id: int
    thread_id: int
    message_id: int


StateErrorFactory = Callable[[str], Exception]
TransactionFactory = Callable[[], AbstractContextManager[None]]
JobHasMaterials = Callable[[str], bool]


class ProviderJobsStateFacade:
    """Provider-job lifecycle on the HubState-owned SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: TransactionFactory,
        write_transaction: TransactionFactory,
        state_error: StateErrorFactory,
        job_has_materials: JobHasMaterials,
        queue_visibility: QueueVisibilityState | None = None,
        selected_codex_profile: Callable[[], str | None] | None = None,
    ) -> None:
        self._connection = connection
        self._transaction = transaction
        self._write_transaction = write_transaction
        self._state_error = state_error
        self._job_has_materials = job_has_materials
        self._queue_visibility = queue_visibility
        self._selected_codex_profile = selected_codex_profile

    def queued_input_group(self, topic_id: int, input_group_key: str) -> ProviderJobRecord | None:
        """Read an album routing candidate without extending its queue hold."""
        key = self._bounded(input_group_key, name="input group key", maximum=256)
        row = self._connection.execute(
            """SELECT * FROM provider_jobs
               WHERE topic_id=? AND input_group_key=? AND status='queued'
                 AND topic_sequence=(
                   SELECT MAX(tail.topic_sequence) FROM provider_jobs tail
                   WHERE tail.topic_id=provider_jobs.topic_id)
               ORDER BY topic_sequence DESC LIMIT 1""",
            (topic_id, key),
        ).fetchone()
        return self.record(row) if row is not None else None

    def _bounded(self, value: str, *, name: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise self._state_error(f"invalid {name}")
        return normalized

    def _timestamp(self, value: datetime | None = None) -> str:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise self._state_error("timestamp must be timezone-aware")
        return current.astimezone(timezone.utc).isoformat()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def record(row: sqlite3.Row) -> ProviderJobRecord:
        return ProviderJobRecord(
            job_id=str(row["job_id"]),
            idempotency_key=str(row["idempotency_key"]),
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            topic_id=int(row["topic_id"]),
            topic_sequence=int(row["topic_sequence"]),
            agent_id=str(row["agent_id"]),
            session_id=str(row["session_id"]),
            session_generation=int(row["session_generation"]),
            provider_session_id=row["provider_session_id"],
            model=str(row["model"]),
            effort=str(row["effort"]),
            payload_text=str(row["payload_text"]),
            context_watermark=row["context_watermark"],
            handoff_id=row["handoff_id"],
            input_group_key=row["input_group_key"],
            codex_permission_profile=row["codex_permission_profile"],
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=row["next_attempt_at"],
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            provider_started_at=row["provider_started_at"],
            error_class=row["error_class"],
            error_code=row["error_code"],
            error_detail=row["error_detail"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def stalled_work_count(self, *, now: datetime) -> int:
        """Count failed leases and old, runnable queue entries without provider access."""
        timestamp = self._timestamp(now)
        stale_before = self._timestamp(now - timedelta(minutes=15))
        expired_leases = int(
            self._connection.execute(
                """SELECT COUNT(*) FROM provider_jobs
                   WHERE status IN ('leased', 'executing')
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (timestamp,),
            ).fetchone()[0]
        )
        # Queue age alone says nothing about a job already being executed.
        # Count only old, due and unblocked work when no live lease occupies
        # the default worker capacity; active work must not cause an error.
        stale_ready_queue = int(
            self._connection.execute(
                """SELECT COUNT(*) FROM provider_jobs candidate
                   JOIN topics candidate_topic ON candidate_topic.topic_id = candidate.topic_id
                   WHERE candidate.status IN ('queued', 'retry_wait')
                     AND candidate.attempt_count < candidate.max_attempts
                     AND ((candidate.status = 'queued'
                           AND candidate.created_at <= ?
                           AND (candidate.next_attempt_at IS NULL
                                OR candidate.next_attempt_at <= ?))
                          OR (candidate.status = 'retry_wait'
                              AND candidate.next_attempt_at <= ?))
                     AND NOT EXISTS (
                       SELECT 1 FROM provider_job_holds held
                       WHERE held.job_id = candidate.job_id AND held.decision = 'pending'
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM provider_jobs earlier
                       WHERE earlier.topic_id = candidate.topic_id
                         AND earlier.topic_sequence < candidate.topic_sequence
                         AND earlier.status NOT IN
                           ('completed', 'failed', 'cancelled', 'indeterminate')
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM provider_jobs earlier
                       JOIN topics earlier_topic ON earlier_topic.topic_id = earlier.topic_id
                       JOIN provider_job_holds held ON held.job_id = earlier.job_id
                       WHERE held.decision = 'pending'
                         AND earlier.status IN ('queued', 'retry_wait')
                         AND COALESCE(earlier_topic.execution_scope,
                                      'project:' || earlier_topic.project_id) =
                             COALESCE(candidate_topic.execution_scope,
                                      'project:' || candidate_topic.project_id)
                         AND (earlier.created_at < candidate.created_at
                              OR (earlier.created_at = candidate.created_at
                                  AND earlier.job_id < candidate.job_id))
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM provider_jobs active
                       WHERE active.status IN ('leased', 'executing')
                         AND active.lease_expires_at > ?
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM provider_jobs uncertain
                       JOIN topics uncertain_topic
                         ON uncertain_topic.topic_id = uncertain.topic_id
                       WHERE uncertain.status = 'indeterminate'
                         AND COALESCE(uncertain_topic.execution_scope,
                                      'project:' || uncertain_topic.project_id) =
                             COALESCE(candidate_topic.execution_scope,
                                      'project:' || candidate_topic.project_id)
                         AND NOT EXISTS (
                           SELECT 1 FROM provider_job_resolutions resolutions
                           WHERE resolutions.job_id = uncertain.job_id
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM provider_turn_terminal_evidence terminal
                           WHERE terminal.job_id = uncertain.job_id
                         )
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM agent_sessions writer
                       JOIN topics writer_topic ON writer_topic.topic_id = writer.topic_id
                       WHERE writer.status IN ('active', 'satellite')
                         AND writer.writer_mode != 'telegram'
                         AND COALESCE(writer_topic.execution_scope,
                                      'project:' || writer_topic.project_id) =
                             COALESCE(candidate_topic.execution_scope,
                                      'project:' || candidate_topic.project_id)
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM turn_dispatches dispatch
                       JOIN topics dispatch_topic ON dispatch_topic.topic_id = dispatch.topic_id
                       WHERE dispatch.status = 'running'
                         AND COALESCE(dispatch_topic.execution_scope,
                                      'project:' || dispatch_topic.project_id) =
                             COALESCE(candidate_topic.execution_scope,
                                      'project:' || candidate_topic.project_id)
                     )""",
                (stale_before, stale_before, stale_before, timestamp),
            ).fetchone()[0]
        )
        return expired_leases + stale_ready_queue

    def topic_has_pending(self, topic_id: int) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM provider_jobs
               WHERE topic_id = ?
                 AND status IN ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
               LIMIT 1""",
            (topic_id,),
        ).fetchone()
        return row is not None

    def topic_has_unheld(self, topic_id: int) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM provider_jobs jobs
               WHERE jobs.topic_id = ?
                 AND jobs.status IN ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
                 AND NOT EXISTS (
                   SELECT 1 FROM provider_job_holds holds WHERE holds.job_id = jobs.job_id
                     AND holds.decision = 'pending'
                 ) LIMIT 1""",
            (topic_id,),
        ).fetchone()
        return row is not None

    def nonterminal_counts(self, agent_ids: Sequence[str]) -> dict[str, int]:
        bounded_ids = tuple(
            self._bounded(agent_id, name="agent id", maximum=64) for agent_id in agent_ids
        )
        if not bounded_ids:
            return {}
        placeholders = ", ".join("?" for _ in bounded_ids)
        rows = self._connection.execute(
            f"""SELECT agent_id, COUNT(*) AS job_count FROM provider_jobs
                WHERE agent_id IN ({placeholders})
                  AND status IN ('queued', 'leased', 'executing', 'retry_wait', 'result_ready')
                GROUP BY agent_id""",
            bounded_ids,
        ).fetchall()
        return {str(row["agent_id"]): int(row["job_count"]) for row in rows}

    def chat_activities(self, agent_ids: Sequence[str]) -> tuple[ProviderChatActivity, ...]:
        bounded_ids = tuple(
            self._bounded(agent_id, name="agent id", maximum=64) for agent_id in agent_ids
        )
        if not bounded_ids:
            return ()
        placeholders = ", ".join("?" for _ in bounded_ids)
        rows = self._connection.execute(
            f"""SELECT current.agent_id, current.chat_id, current.message_id, topics.thread_id
                FROM provider_jobs current
                JOIN topics ON topics.topic_id = current.topic_id
                WHERE current.agent_id IN ({placeholders})
                  AND current.status IN ('queued', 'leased', 'executing', 'result_ready')
                  AND NOT EXISTS (
                    SELECT 1 FROM provider_job_holds holds
                    WHERE holds.job_id=current.job_id AND holds.decision='pending'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM provider_jobs earlier
                    WHERE earlier.topic_id = current.topic_id
                      AND earlier.topic_sequence < current.topic_sequence
                      AND earlier.status NOT IN (
                        'completed', 'failed', 'cancelled', 'indeterminate'
                      )
                  )
                ORDER BY current.created_at, current.topic_id""",
            bounded_ids,
        ).fetchall()
        return tuple(
            ProviderChatActivity(
                agent_id=str(row["agent_id"]),
                chat_id=int(row["chat_id"]),
                thread_id=int(row["thread_id"]),
                message_id=int(row["message_id"]),
            )
            for row in rows
        )

    def pending_batch_agent(self, topic_id: int, *, now: datetime | None = None) -> str | None:
        timestamp = self._timestamp(now)
        row = self._connection.execute(
            """SELECT agent_id FROM provider_jobs
               WHERE topic_id = ? AND status = 'queued' AND next_attempt_at > ?
                 AND topic_sequence = (
                   SELECT MAX(tail.topic_sequence) FROM provider_jobs tail
                   WHERE tail.topic_id = provider_jobs.topic_id
                 )
               LIMIT 1""",
            (topic_id, timestamp),
        ).fetchone()
        return str(row["agent_id"]) if row is not None else None

    def get(self, job_id: str) -> ProviderJobRecord:
        row = self._connection.execute(
            "SELECT * FROM provider_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise self._state_error(f"unknown provider job: {job_id}")
        return self.record(row)

    def for_topic(self, topic_id: int) -> tuple[ProviderJobRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM provider_jobs WHERE topic_id = ? ORDER BY topic_sequence",
            (topic_id,),
        ).fetchall()
        return tuple(self.record(row) for row in rows)

    def flush_batch(self, topic_id: int) -> int:
        timestamp = self._now()
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs SET next_attempt_at = ?, updated_at = ?
                   WHERE topic_id = ? AND status = 'queued'
                     AND next_attempt_at IS NOT NULL AND next_attempt_at > ?""",
                (timestamp, timestamp, topic_id, timestamp),
            )
        return cursor.rowcount

    def resolve_indeterminate(self, job_id: str, resolution: str) -> bool:
        identifier = self._bounded(job_id, name="provider job id", maximum=128)
        classification = self._bounded(resolution, name="resolution", maximum=32)
        if classification not in {"acknowledged", "superseded", "externally_completed"}:
            raise self._state_error("invalid indeterminate job resolution")
        with self._transaction():
            job = self._connection.execute(
                "SELECT status FROM provider_jobs WHERE job_id = ?", (identifier,)
            ).fetchone()
            if job is None:
                raise self._state_error(f"unknown provider job: {identifier}")
            if str(job["status"]) != "indeterminate":
                raise self._state_error("only an indeterminate provider job can be resolved")
            existing = self._connection.execute(
                "SELECT resolution FROM provider_job_resolutions WHERE job_id = ?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                if str(existing["resolution"]) == classification:
                    self._complete_stops_after(identifier, self._now())
                    return False
                raise self._state_error(
                    "indeterminate provider job already has a different resolution"
                )
            self._connection.execute(
                """INSERT INTO provider_job_resolutions (job_id, resolution, resolved_at)
                   VALUES (?, ?, ?)""",
                (identifier, classification, self._now()),
            )
            self._complete_stops_after(identifier, self._now())
        return True

    def cancel_active(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str = "emergency_stop",
        complete_stops: bool = False,
    ) -> None:
        """Cancel a stopped job and, optionally, complete the stops covering it.

        Both happen in one transaction, so a worker that dies right after the
        cancellation never leaves a stop pending, where it would keep blocking
        the topic. Every pending stop covering the job completes once none of
        its other covered work can still run, so a repeated stop message does
        not outlive the work it stopped and no stop ends before its work. Work
        that a stop has already cancelled, at its result commit for example,
        is left as it is.
        """
        with self._transaction():
            stopped = self._cancel_stopped(
                job_id,
                lease_token,
                statuses=("leased", "executing"),
                error_code=error_code,
                complete_stops=complete_stops,
                timestamp=self._now(),
            )
            if not stopped and not self._ended_by_stop(job_id):
                raise self._state_error("active provider job cannot be cancelled")

    def honor_stop(self, job_id: str, lease_token: str, status: str) -> bool:
        """Cancel the job for a covering pending stop instead of committing it.

        The result and failure commits call this first, inside their own write
        transaction, so each commit is the last stop check (R-021): a stop
        recorded after the worker's final check still ends the job, whose
        single outbox row stays free for the stop's Hub acknowledgement, and
        the stops left without work complete with it. The cancellation takes
        its time here, under the write lock, so it is never earlier than the
        stop and the stop's notice can still choose the job. True when a stop
        has ended the job, now or earlier; False leaves the commit to proceed.
        """
        if self._ended_by_stop(job_id):
            return True
        if self.pending_stop_for_job(job_id) is None:
            return False
        return self._cancel_stopped(
            job_id,
            lease_token,
            statuses=(status,),
            error_code="emergency_stop",
            complete_stops=True,
            timestamp=self._now(),
        )

    def _ended_by_stop(self, job_id: str) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM provider_jobs
               WHERE job_id = ? AND status = 'cancelled' AND error_class = 'user_stop'""",
            (job_id,),
        ).fetchone()
        return row is not None

    def pending_stop_for_job(self, job_id: str) -> str | None:
        """Return the oldest pending emergency stop that covers this job."""
        return pending_stop_for_job(self._connection, job_id)

    def stop_notice_job(self, request_id: str) -> sqlite3.Row | None:
        """Choose the covered job that carries a stop's Hub acknowledgement.

        Only covered work qualifies, so a repeated stop message never picks a
        job that started after the stop. Returns ``job_id`` and ``thread_id``.
        """
        return self._connection.execute(
            f"""SELECT job.job_id, topics.thread_id
                FROM provider_stop_requests stop
                JOIN provider_jobs job ON job.topic_id = stop.topic_id
                JOIN topics ON topics.topic_id = job.topic_id
                WHERE stop.request_id = ? AND {STOP_COVERS_JOB_SQL} AND (
                    (job.agent_id = stop.target_agent_id
                     AND job.status IN ('leased', 'executing')) OR (
                        job.status = 'cancelled'
                        AND job.error_class = 'user_stop'
                        AND job.error_code = 'emergency_stop'
                        AND job.updated_at >= stop.created_at
                    )
                )
                ORDER BY CASE WHEN job.status IN ('leased', 'executing') THEN 0 ELSE 1 END,
                         job.updated_at DESC, job.created_at DESC
                LIMIT 1""",
            (request_id,),
        ).fetchone()

    def _cancel_stopped(
        self,
        job_id: str,
        lease_token: str,
        *,
        statuses: tuple[str, ...],
        error_code: str,
        complete_stops: bool,
        timestamp: str,
    ) -> bool:
        placeholders = ", ".join("?" for _ in statuses)
        cursor = self._connection.execute(
            f"""UPDATE provider_jobs
               SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                   lease_expires_at = NULL, next_attempt_at = NULL,
                   error_class = 'user_stop', error_code = ?, updated_at = ?
               WHERE job_id = ? AND lease_token = ? AND status IN ({placeholders})""",
            (error_code, timestamp, job_id, lease_token, *statuses),
        )
        if cursor.rowcount != 1:
            return False
        self._sync_visibility(job_id, timestamp)
        if complete_stops:
            self._complete_stops_after(job_id, timestamp)
        return True

    def complete_finished_stops(self, topic_id: int, timestamp: str) -> None:
        """Complete the topic's pending stops whose covered work is all over.

        The caller holds the write transaction.
        """
        self._connection.execute(COMPLETE_FINISHED_STOPS_SQL, (timestamp, topic_id))

    def _complete_stops_after(self, job_id: str, timestamp: str) -> None:
        """Complete the stops that the end of this job left without work.

        The transitions that end leased, waiting or running work call this in
        their own transaction, so a stop completes however its covered work
        ended, not only when it was cancelled (R-021); ``cancel_active`` does
        so when its caller asks.
        """
        topic = self._connection.execute(
            "SELECT topic_id FROM provider_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if topic is not None:
            self.complete_finished_stops(int(topic["topic_id"]), timestamp)
            self._sync_visibility(job_id, timestamp)

    def _sync_visibility(self, job_id: str, timestamp: str) -> None:
        if self._queue_visibility is not None:
            self._queue_visibility.sync_job_in_transaction(
                job_id, now=datetime.fromisoformat(timestamp)
            )

    def cancel_unstarted_for_stop(self, topic_id: int, timestamp: str) -> int:
        """Cancel the topic's queued and retry-waiting jobs that are not held.

        Held jobs wait for the owner's decision. The caller holds the write
        transaction of the stop request.
        """
        cursor = self._connection.execute(
            """UPDATE provider_jobs
               SET status = 'cancelled', next_attempt_at = NULL,
                   error_class = 'user_stop', error_code = 'emergency_stop',
                   updated_at = ?
               WHERE topic_id = ? AND status IN ('queued', 'retry_wait')
                 AND NOT EXISTS (SELECT 1 FROM provider_job_holds held WHERE
                   held.job_id = provider_jobs.job_id AND held.decision = 'pending')""",
            (timestamp, topic_id),
        )
        if self._queue_visibility is not None and self._queue_visibility.available:
            for row in self._connection.execute(
                "SELECT DISTINCT job.job_id FROM provider_jobs job "
                "JOIN task_lifecycle_notices notice ON notice.job_id=job.job_id "
                "WHERE job.topic_id=? AND job.status='cancelled' "
                "AND notice.kind IN ('accepted','queued','executing') "
                "AND notice.status IN ('pending','leased') AND notice.attempt_count=0",
                (topic_id,),
            ).fetchall():
                self._sync_visibility(str(row["job_id"]), timestamp)
        return cursor.rowcount

    def lease(
        self,
        agent_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 90,
        max_parallel_roots: int = 1,
        scheduler_agents: Sequence[str] = (),
        agent_capacities: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> ProviderJobRecord | None:
        target_agent = self._bounded(agent_id, name="agent id", maximum=64)
        worker = self._bounded(worker_id, name="worker id", maximum=128)
        if not 1 <= lease_seconds <= 3600:
            raise self._state_error("invalid provider lease duration")
        if not 1 <= max_parallel_roots <= 16:
            raise self._state_error("invalid parallel root capacity")
        scheduled_agents = tuple(
            self._bounded(value, name="scheduler agent id", maximum=64)
            for value in scheduler_agents
        )
        if len(set(scheduled_agents)) != len(scheduled_agents):
            raise self._state_error("scheduler agents contain duplicates")
        if scheduled_agents and target_agent not in scheduled_agents:
            raise self._state_error("scheduler agents must include the target agent")
        capacities = {
            self._bounded(key, name="capacity agent id", maximum=64): value
            for key, value in (agent_capacities or {}).items()
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16
            for value in capacities.values()
        ):
            raise self._state_error("invalid agent capacity")
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        expires_at = self._timestamp(current + timedelta(seconds=lease_seconds))
        with self._transaction():
            freshness = self._timestamp(current - PROVIDER_WORKER_FAIRNESS_FRESHNESS)
            placeholders = ", ".join("?" for _ in scheduled_agents)
            if scheduled_agents:
                # Schema 32 keys declarations by text. Canonical parallel slots
                # use distinct keys so a rolling reduction cannot be overwritten
                # by another old process. The legacy agent key stays in the
                # read set until it ages out after an upgrade.
                slot_declarations = _parallel_worker_declarations(target_agent)
                declaration_key = worker if worker in slot_declarations else target_agent
                self._connection.execute(
                    """INSERT INTO execution_scheduler_workers
                       (agent_id, declared_capacity, observed_at) VALUES (?, ?, ?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         declared_capacity = excluded.declared_capacity,
                         observed_at = excluded.observed_at""",
                    (declaration_key, max_parallel_roots, timestamp),
                )
            capacity = read_queue_capacity(
                self._connection,
                QueueCapacityConfig(max_parallel_roots, scheduled_agents, capacities),
                now=current,
            )
            if capacity.occupied_roots >= capacity.effective_capacity:
                return None
            busy_agents = capacity.busy_agents
            busy_workers = capacity.busy_workers
            if worker in busy_workers or busy_agents.get(target_agent, 0) >= capacities.get(
                target_agent, 1
            ):
                return None
            row = self._connection.execute(
                _ELIGIBLE_PROVIDER_JOB_SQL,
                (target_agent, timestamp, timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            if scheduled_agents:
                health_rows = self._connection.execute(
                    f"""SELECT DISTINCT agent_id, instance_id FROM runtime_health
                         WHERE component = 'provider_worker'
                           AND agent_id IN ({placeholders})
                           AND active_job_id IS NULL
                           AND heartbeat_at >= ?""",
                    (*scheduled_agents, freshness),
                ).fetchall()
                live_idle_agents = {target_agent}
                for item in health_rows:
                    contender_agent = str(item["agent_id"])
                    instance_id = str(item["instance_id"])
                    expected_ids = (
                        _parallel_worker_declarations(contender_agent)[
                            : capacities.get(contender_agent, 1)
                        ]
                        if contender_agent in {"codex", "claude"}
                        else (f"{contender_agent}-worker",)
                    )
                    if instance_id in expected_ids and instance_id not in busy_workers:
                        live_idle_agents.add(contender_agent)
                contenders: list[tuple[int, str, int, int, sqlite3.Row]] = []
                for contender_agent in sorted(live_idle_agents.intersection(scheduled_agents)):
                    if busy_agents.get(contender_agent, 0) >= capacities.get(contender_agent, 1):
                        continue
                    candidate = self._connection.execute(
                        _ELIGIBLE_PROVIDER_JOB_SQL,
                        (contender_agent, timestamp, timestamp, timestamp),
                    ).fetchone()
                    if candidate is None:
                        continue
                    grant = self._connection.execute(
                        """SELECT last_grant_sequence FROM execution_scheduler_grants
                           WHERE agent_id = ?""",
                        (contender_agent,),
                    ).fetchone()
                    contenders.append(
                        (
                            0 if grant is None else int(grant["last_grant_sequence"]),
                            str(candidate["created_at"]),
                            int(candidate["topic_id"]),
                            int(candidate["topic_sequence"]),
                            candidate,
                        )
                    )
                if not contenders:
                    return None
                winner = min(contenders, key=lambda item: item[:4])
                if str(winner[4]["agent_id"]) != target_agent:
                    return None
                row = winner[4]
            token = str(uuid.uuid4())
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'leased', lease_owner = ?, lease_token = ?,
                       lease_expires_at = ?, next_attempt_at = NULL,
                       error_class = NULL, error_code = NULL, error_detail = NULL,
                       updated_at = ?
                   WHERE job_id = ? AND status IN ('queued', 'retry_wait')""",
                (worker, token, expires_at, timestamp, row["job_id"]),
            )
            if cursor.rowcount != 1:
                raise self._state_error("provider job lease race")
            if scheduled_agents:
                next_grant = int(
                    self._connection.execute(
                        """SELECT COALESCE(MAX(last_grant_sequence), 0) + 1
                           FROM execution_scheduler_grants"""
                    ).fetchone()[0]
                )
                self._connection.execute(
                    """INSERT INTO execution_scheduler_grants
                       (agent_id, last_grant_sequence, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         last_grant_sequence = excluded.last_grant_sequence,
                         updated_at = excluded.updated_at""",
                    (target_agent, next_grant, timestamp),
                )
            leased = self._connection.execute(
                "SELECT * FROM provider_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            if leased is None:
                raise self._state_error("leased provider job disappeared")
            return self.record(leased)

    def lease_steer_followup(
        self,
        parent_job_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> ProviderJobRecord | None:
        worker = self._bounded(worker_id, name="worker id", maximum=128)
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        expires_at = self._timestamp(current + timedelta(seconds=lease_seconds))
        with self._transaction():
            parent = self._connection.execute(
                "SELECT * FROM provider_jobs WHERE job_id = ? AND status = 'executing'",
                (parent_job_id,),
            ).fetchone()
            # Nothing joins a turn that a pending emergency stop is ending.
            if parent is None or self.pending_stop_for_job(parent_job_id) is not None:
                return None
            # Managed steering lacks an independently verified active-policy read.
            # Keep the follow-up queued for normal exact-profile preparation.
            if (
                self._selected_codex_profile is None
                or parent["codex_permission_profile"] != self._selected_codex_profile()
                or parent["codex_permission_profile"] is not None
            ):
                return None
            candidate = self._connection.execute(
                """SELECT * FROM provider_jobs
                   WHERE topic_id = ? AND topic_sequence = (
                       SELECT MIN(topic_sequence) FROM provider_jobs
                       WHERE topic_id = ? AND topic_sequence > ?
                         AND status NOT IN ('completed', 'failed', 'cancelled', 'indeterminate')
                   )""",
                (parent["topic_id"], parent["topic_id"], parent["topic_sequence"]),
            ).fetchone()
            if candidate is None or any(
                candidate[field] != parent[field]
                for field in (
                    "agent_id",
                    "session_id",
                    "session_generation",
                    "model",
                    "effort",
                    "codex_permission_profile",
                )
            ):
                return None
            if str(candidate["status"]) != "queued":
                return None
            held = self._connection.execute(
                """SELECT 1 FROM provider_job_holds
                   WHERE job_id = ? AND decision = 'pending'""",
                (candidate["job_id"],),
            ).fetchone()
            # Held work waits for the owner's decision, also when it could steer.
            if held is not None or self._job_has_materials(str(candidate["job_id"])):
                return None
            available = candidate["next_attempt_at"]
            if available is not None and str(available) > timestamp:
                return None
            token = str(uuid.uuid4())
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'leased', lease_owner = ?, lease_token = ?,
                       lease_expires_at = ?, next_attempt_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status = 'queued'""",
                (worker, token, expires_at, timestamp, candidate["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            return self.get(str(candidate["job_id"]))

    def reject_unaccepted_steer(self, job_id: str, lease_token: str) -> None:
        """Requeue an executing child only after proven rejection or no RPC attempt."""
        timestamp = self._now()
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'queued', attempt_count = MAX(0, attempt_count - 1),
                       provider_started_at = NULL, lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status = 'executing' AND lease_token = ?""",
                (timestamp, job_id, lease_token),
            )
        if cursor.rowcount != 1:
            raise self._state_error("rejected steer job lease is missing or invalid")

    def complete_steered(
        self,
        child_job_id: str,
        lease_token: str,
        *,
        parent_job_id: str,
        provider_turn_id: str,
    ) -> None:
        turn_id = self._bounded(provider_turn_id, name="provider turn id", maximum=256)
        timestamp = self._now()
        with self._transaction():
            child = self._connection.execute(
                """SELECT * FROM provider_jobs WHERE job_id = ? AND status = 'executing'
                   AND lease_token = ?""",
                (child_job_id, lease_token),
            ).fetchone()
            parent = self._connection.execute(
                "SELECT * FROM provider_jobs WHERE job_id = ? AND status = 'executing'",
                (parent_job_id,),
            ).fetchone()
            if (
                child is None
                or parent is None
                or any(
                    child[field] != parent[field]
                    for field in ("topic_id", "agent_id", "session_id", "session_generation")
                )
            ):
                raise self._state_error("steered job does not match its active parent")
            self._connection.execute(
                """INSERT INTO provider_job_absorptions
                   (child_job_id, parent_job_id, provider_turn_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (child_job_id, parent_job_id, turn_id, timestamp),
            )
            if child["context_watermark"] is not None:
                self._connection.execute(
                    """INSERT INTO visible_context_cursors
                       (topic_id, observer_agent_id, last_turn_id, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(topic_id, observer_agent_id) DO UPDATE SET
                         last_turn_id = MAX(last_turn_id, excluded.last_turn_id),
                         updated_at = excluded.updated_at""",
                    (
                        child["topic_id"],
                        child["agent_id"],
                        child["context_watermark"],
                        timestamp,
                    ),
                )
            if child["handoff_id"] is not None:
                self._connection.execute(
                    "DELETE FROM pending_handoffs WHERE handoff_id = ?",
                    (child["handoff_id"],),
                )
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'completed', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status = 'executing' AND lease_token = ?""",
                (timestamp, child_job_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise self._state_error("steered job lease changed during completion")
            self._complete_stops_after(child_job_id, timestamp)

    def mark_executing(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
        honor_stop: bool = False,
    ) -> ProviderJobRecord:
        """Start a leased job, or with ``honor_stop`` cancel it before the provider runs.

        A pending emergency stop that covers the job is checked inside the same
        immediate transaction as the start: a stop committed first always
        cancels the job before the provider runs, and a stop committed later
        finds it executing and interrupts it.
        """
        with self._transaction():
            timestamp = self._timestamp(now)
            if honor_stop and self.pending_stop_for_job(job_id) is not None:
                changed = self._cancel_stopped(
                    job_id,
                    lease_token,
                    statuses=("leased",),
                    error_code="emergency_stop",
                    complete_stops=True,
                    timestamp=timestamp,
                )
            else:
                changed = self._start_leased(job_id, lease_token, timestamp)
            if not changed:
                raise self._state_error("provider job lease is missing, expired, or invalid")
            return self.get(job_id)

    def start_steer(
        self,
        job_id: str,
        lease_token: str,
        *,
        parent_job_id: str,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        """Start a leased follow-up that is about to join its parent's running turn.

        Everything is decided in the transaction of the start. A follow-up an
        emergency stop covers is cancelled; the stop completes only once none
        of its covered work, such as the parent turn, can still run. A follow-up
        the stop does not cover returns to the queue when a stop covers the
        parent or the parent no longer runs, and later runs as its own turn
        instead of joining a turn that is being stopped or has ended.
        """
        with self._transaction():
            timestamp = self._timestamp(now)
            parent = self._connection.execute(
                "SELECT status FROM provider_jobs WHERE job_id = ?", (parent_job_id,)
            ).fetchone()
            parent_running = parent is not None and str(parent["status"]) == "executing"
            if self.pending_stop_for_job(job_id) is not None:
                changed = self._cancel_stopped(
                    job_id,
                    lease_token,
                    statuses=("leased",),
                    error_code="emergency_stop",
                    complete_stops=True,
                    timestamp=timestamp,
                )
            elif not parent_running or self.pending_stop_for_job(parent_job_id) is not None:
                changed = (
                    self._connection.execute(
                        """UPDATE provider_jobs
                           SET status = 'queued', lease_owner = NULL, lease_token = NULL,
                               lease_expires_at = NULL, updated_at = ?
                           WHERE job_id = ? AND status = 'leased' AND lease_token = ?""",
                        (timestamp, job_id, lease_token),
                    ).rowcount
                    == 1
                )
            else:
                changed = self._start_leased(job_id, lease_token, timestamp)
            if not changed:
                raise self._state_error("provider job lease is missing, expired, or invalid")
            return self.get(job_id)

    def _start_leased(self, job_id: str, lease_token: str, timestamp: str) -> bool:
        cursor = self._connection.execute(
            """UPDATE provider_jobs
               SET status = 'executing', attempt_count = attempt_count + 1,
                   provider_started_at = ?, updated_at = ?
               WHERE job_id = ? AND status = 'leased' AND lease_token = ?
                 AND lease_expires_at > ? AND attempt_count < max_attempts""",
            (timestamp, timestamp, job_id, lease_token, timestamp),
        )
        if cursor.rowcount == 1 and self._queue_visibility is not None:
            self._queue_visibility.executing_in_transaction(
                job_id, now=datetime.fromisoformat(timestamp)
            )
        return cursor.rowcount == 1

    def release_lease(self, job_id: str, lease_token: str) -> None:
        timestamp = self._now()
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'queued', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status = 'leased' AND lease_token = ?""",
                (timestamp, job_id, lease_token),
            )
        if cursor.rowcount != 1:
            raise self._state_error("provider job lease is missing or invalid")

    def heartbeat(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        if not 1 <= lease_seconds <= 3600:
            raise self._state_error("invalid provider lease duration")
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        expires_at = self._timestamp(current + timedelta(seconds=lease_seconds))
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs SET lease_expires_at = ?, updated_at = ?
                   WHERE job_id = ? AND status IN ('leased', 'executing')
                     AND lease_token = ? AND lease_expires_at > ?""",
                (expires_at, timestamp, job_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise self._state_error("provider job lease is missing, expired, or invalid")
        return self.get(job_id)

    def schedule_retry(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: int,
        error_detail: str | None = None,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        code = self._bounded(error_code, name="error code", maximum=128)
        if not 0 <= delay_seconds <= 86400:
            raise self._state_error("invalid retry delay")
        detail = error_detail.strip()[:1000] if error_detail else None
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        available_at = self._timestamp(current + timedelta(seconds=delay_seconds))
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = CASE
                         WHEN attempt_count + 1 >= max_attempts THEN 'failed'
                         ELSE 'retry_wait'
                       END,
                       attempt_count = attempt_count + 1,
                       next_attempt_at = CASE
                         WHEN attempt_count + 1 >= max_attempts THEN NULL
                         ELSE ?
                       END,
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       error_class = 'transient_pre_execution', error_code = ?,
                       error_detail = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'leased' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (available_at, code, detail, timestamp, job_id, lease_token, timestamp),
            )
            self._complete_stops_after(job_id, timestamp)
        if cursor.rowcount != 1:
            raise self._state_error("retry requires a current pre-execution provider job lease")
        return self.get(job_id)

    def fail(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_code: str,
        error_detail: str | None = None,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        failure_class = self._bounded(error_class, name="error class", maximum=64)
        code = self._bounded(error_code, name="error code", maximum=128)
        detail = error_detail.strip()[:1000] if error_detail else None
        timestamp = self._timestamp(now)
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, error_class = ?, error_code = ?,
                       error_detail = ?, updated_at = ?
                   WHERE job_id = ? AND status IN ('leased', 'executing')
                     AND lease_token = ? AND lease_expires_at > ?""",
                (failure_class, code, detail, timestamp, job_id, lease_token, timestamp),
            )
            self._complete_stops_after(job_id, timestamp)
        if cursor.rowcount != 1:
            raise self._state_error("provider job lease is missing or invalid")
        return self.get(job_id)

    def mark_indeterminate(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        error_detail: str | None = None,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        code = self._bounded(error_code, name="error code", maximum=128)
        detail = error_detail.strip()[:1000] if error_detail else None
        timestamp = self._timestamp(now)
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'indeterminate', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, error_class = 'ambiguous_execution',
                       error_code = ?, error_detail = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'executing' AND lease_token = ?
                     AND lease_expires_at > ?""",
                (code, detail, timestamp, job_id, lease_token, timestamp),
            )
            self._complete_stops_after(job_id, timestamp)
        if cursor.rowcount != 1:
            raise self._state_error("provider job lease is missing or invalid")
        return self.get(job_id)

    def cancel(self, job_id: str) -> ProviderJobRecord:
        timestamp = self._now()
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'cancelled', next_attempt_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status IN ('queued', 'retry_wait')""",
                (timestamp, job_id),
            )
            self._complete_stops_after(job_id, timestamp)
        if cursor.rowcount != 1:
            raise self._state_error("only queued provider work can be cancelled")
        return self.get(job_id)

    def recover_stale(
        self, *, agent_id: str | None = None, now: datetime | None = None
    ) -> ProviderJobRecovery:
        target_agent = self._bounded(agent_id, name="agent id", maximum=64) if agent_id else None
        timestamp = self._timestamp(now)
        scope = "AND agent_id = ?" if target_agent is not None else ""
        params: tuple[object, ...] = (timestamp,)
        if target_agent is not None:
            params += (target_agent,)
        with self._transaction():
            leased = self._connection.execute(
                "SELECT job_id FROM provider_jobs "
                "WHERE status = 'leased' AND lease_expires_at <= ? " + scope + " ORDER BY job_id",
                params,
            ).fetchall()
            executing = self._connection.execute(
                "SELECT job_id, topic_id FROM provider_jobs "
                "WHERE status = 'executing' AND lease_expires_at <= ? "
                + scope
                + " ORDER BY job_id",
                params,
            ).fetchall()
            self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'queued', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, next_attempt_at = NULL,
                       error_class = 'recovered_pre_execution',
                       error_code = 'stale_lease', updated_at = ?
                   WHERE status = 'leased' AND lease_expires_at <= ? """
                + scope,
                (timestamp, timestamp) + ((target_agent,) if target_agent is not None else ()),
            )
            self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'indeterminate', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, error_class = 'ambiguous_execution',
                       error_code = 'stale_executing_lease', updated_at = ?
                   WHERE status = 'executing' AND lease_expires_at <= ? """
                + scope,
                (timestamp, timestamp) + ((target_agent,) if target_agent is not None else ()),
            )
            for topic_id in sorted({int(row["topic_id"]) for row in executing}):
                self.complete_finished_stops(topic_id, timestamp)
            for row in executing:
                self._sync_visibility(str(row["job_id"]), timestamp)
        return ProviderJobRecovery(
            requeued_job_ids=tuple(str(row["job_id"]) for row in leased),
            indeterminate_job_ids=tuple(str(row["job_id"]) for row in executing),
        )
