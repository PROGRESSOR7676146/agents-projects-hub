from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

PROVIDER_WORKER_FAIRNESS_FRESHNESS = timedelta(minutes=2)

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
    ) -> None:
        self._connection = connection
        self._transaction = transaction
        self._write_transaction = write_transaction
        self._state_error = state_error
        self._job_has_materials = job_has_materials

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
                    return False
                raise self._state_error(
                    "indeterminate provider job already has a different resolution"
                )
            self._connection.execute(
                """INSERT INTO provider_job_resolutions (job_id, resolution, resolved_at)
                   VALUES (?, ?, ?)""",
                (identifier, classification, self._now()),
            )
        return True

    def cancel_active(
        self, job_id: str, lease_token: str, *, error_code: str = "emergency_stop"
    ) -> None:
        timestamp = self._now()
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, next_attempt_at = NULL,
                       error_class = 'user_stop', error_code = ?, updated_at = ?
                   WHERE job_id = ? AND lease_token = ?
                     AND status IN ('leased', 'executing')""",
                (error_code, timestamp, job_id, lease_token),
            )
        if cursor.rowcount != 1:
            raise self._state_error("active provider job cannot be cancelled")

    def lease(
        self,
        agent_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 90,
        max_parallel_roots: int = 1,
        scheduler_agents: Sequence[str] = (),
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
        current = now or datetime.now(timezone.utc)
        timestamp = self._timestamp(current)
        expires_at = self._timestamp(current + timedelta(seconds=lease_seconds))
        with self._transaction():
            effective_capacity = max_parallel_roots
            freshness = self._timestamp(current - PROVIDER_WORKER_FAIRNESS_FRESHNESS)
            placeholders = ", ".join("?" for _ in scheduled_agents)
            if scheduled_agents:
                self._connection.execute(
                    """INSERT INTO execution_scheduler_workers
                       (agent_id, declared_capacity, observed_at) VALUES (?, ?, ?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         declared_capacity = excluded.declared_capacity,
                         observed_at = excluded.observed_at""",
                    (target_agent, max_parallel_roots, timestamp),
                )
                advertised = self._connection.execute(
                    f"""SELECT MIN(declared_capacity) FROM execution_scheduler_workers
                         WHERE agent_id IN ({placeholders}) AND observed_at >= ?""",
                    (*scheduled_agents, freshness),
                ).fetchone()[0]
                if advertised is not None:
                    effective_capacity = min(effective_capacity, int(advertised))
            occupied = int(
                self._connection.execute(
                    """SELECT COUNT(DISTINCT COALESCE(
                         topics.execution_scope, 'project:' || topics.project_id))
                       FROM provider_jobs jobs
                       JOIN topics ON topics.topic_id = jobs.topic_id
                       WHERE jobs.status IN ('leased', 'executing')
                         AND jobs.lease_expires_at > ?""",
                    (timestamp,),
                ).fetchone()[0]
            )
            if occupied >= effective_capacity:
                return None
            busy_agents = {
                str(item["agent_id"])
                for item in self._connection.execute(
                    """SELECT DISTINCT agent_id FROM provider_jobs
                       WHERE status IN ('leased', 'executing')
                         AND lease_expires_at > ?""",
                    (timestamp,),
                ).fetchall()
            }
            if target_agent in busy_agents:
                return None
            row = self._connection.execute(
                _ELIGIBLE_PROVIDER_JOB_SQL,
                (target_agent, timestamp, timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            if scheduled_agents:
                health_rows = self._connection.execute(
                    f"""SELECT DISTINCT agent_id FROM runtime_health
                         WHERE component = 'provider_worker'
                           AND agent_id IN ({placeholders})
                           AND heartbeat_at >= ?""",
                    (*scheduled_agents, freshness),
                ).fetchall()
                live_agents = {target_agent}
                live_agents.update(str(item["agent_id"]) for item in health_rows)
                contenders: list[tuple[int, str, int, int, sqlite3.Row]] = []
                for contender_agent in sorted(live_agents.intersection(scheduled_agents)):
                    if contender_agent in busy_agents:
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
            if parent is None:
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
                for field in ("agent_id", "session_id", "session_generation", "model", "effort")
            ):
                return None
            if str(candidate["status"]) != "queued":
                return None
            if self._job_has_materials(str(candidate["job_id"])):
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

    def mark_executing(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> ProviderJobRecord:
        timestamp = self._timestamp(now)
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'executing', attempt_count = attempt_count + 1,
                       provider_started_at = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'leased' AND lease_token = ?
                     AND lease_expires_at > ? AND attempt_count < max_attempts""",
                (timestamp, timestamp, job_id, lease_token, timestamp),
            )
        if cursor.rowcount != 1:
            raise self._state_error("provider job lease is missing, expired, or invalid")
        return self.get(job_id)

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
        if cursor.rowcount != 1:
            raise self._state_error("provider job lease is missing or invalid")
        return self.get(job_id)

    def cancel(self, job_id: str) -> ProviderJobRecord:
        with self._write_transaction():
            cursor = self._connection.execute(
                """UPDATE provider_jobs
                   SET status = 'cancelled', next_attempt_at = NULL, updated_at = ?
                   WHERE job_id = ? AND status IN ('queued', 'retry_wait')""",
                (self._now(), job_id),
            )
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
                "SELECT job_id FROM provider_jobs "
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
        return ProviderJobRecovery(
            requeued_job_ids=tuple(str(row["job_id"]) for row in leased),
            indeterminate_job_ids=tuple(str(row["job_id"]) for row in executing),
        )
