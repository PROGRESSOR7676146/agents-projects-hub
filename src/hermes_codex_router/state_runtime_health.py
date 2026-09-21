from __future__ import annotations

import re
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .release_identity import CURRENT_RELEASE, ReleaseIdentity
from .telegram import TELEGRAM_HEALTH_FAILURE_THRESHOLD


@dataclass(frozen=True, slots=True)
class RuntimeHealthRecord:
    component: str
    instance_id: str
    runtime: str | None
    agent_id: str | None
    pid: int
    process_start_marker: str
    started_at: str
    heartbeat_at: str
    success_at: str | None
    error_code: str | None
    activity_state: str
    active_job_id: str | None
    active_lease_expires_at: str | None
    provider_state: str
    quota_remaining_percent: float | None
    quota_reset_at: str | None
    release_version: str | None
    release_git_sha: str | None
    release_built_at: str | None
    release_clean: bool
    transport_operation: str | None
    transport_failure_class: str | None
    transport_status_code: int | None
    transport_retry_after: int | None
    transport_consecutive_failures: int
    transport_success_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class RuntimeHealthStatus:
    status: str
    record: RuntimeHealthRecord | None


StateErrorFactory = Callable[[str], Exception]
TransactionFactory = Callable[[], AbstractContextManager[None]]


class RuntimeHealthStateFacade:
    """Passive runtime-health snapshots on the HubState-owned connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        write_transaction: TransactionFactory,
        state_error: StateErrorFactory,
    ) -> None:
        self._connection = connection
        self._write_transaction = write_transaction
        self._state_error = state_error

    def _bounded(self, value: str, *, name: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise self._state_error(f"invalid {name}")
        return normalized

    def _optional_bounded(self, value: str | None, *, name: str, maximum: int) -> str | None:
        if value is None:
            return None
        return self._bounded(value, name=name, maximum=maximum)

    def _timestamp(self, value: datetime | None = None) -> str:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise self._state_error("timestamp must be timezone-aware")
        return current.astimezone(timezone.utc).isoformat()

    def _parse_timestamp(self, value: str, *, name: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise self._state_error(f"invalid {name}") from exc
        if parsed.tzinfo is None:
            raise self._state_error(f"{name} must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def record(row: sqlite3.Row) -> RuntimeHealthRecord:
        return RuntimeHealthRecord(
            component=str(row["component"]),
            instance_id=str(row["instance_id"]),
            runtime=None if row["runtime"] is None else str(row["runtime"]),
            agent_id=None if row["agent_id"] is None else str(row["agent_id"]),
            pid=int(row["pid"]),
            process_start_marker=str(row["process_start_marker"]),
            started_at=str(row["started_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            success_at=None if row["success_at"] is None else str(row["success_at"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            activity_state=str(row["activity_state"]),
            active_job_id=(None if row["active_job_id"] is None else str(row["active_job_id"])),
            active_lease_expires_at=(
                None
                if row["active_lease_expires_at"] is None
                else str(row["active_lease_expires_at"])
            ),
            provider_state=str(row["provider_state"]),
            quota_remaining_percent=(
                None
                if row["quota_remaining_percent"] is None
                else float(row["quota_remaining_percent"])
            ),
            quota_reset_at=(None if row["quota_reset_at"] is None else str(row["quota_reset_at"])),
            release_version=(
                None if row["release_version"] is None else str(row["release_version"])
            ),
            release_git_sha=(
                None if row["release_git_sha"] is None else str(row["release_git_sha"])
            ),
            release_built_at=(
                None if row["release_built_at"] is None else str(row["release_built_at"])
            ),
            release_clean=bool(row["release_clean"]),
            transport_operation=(
                None if row["transport_operation"] is None else str(row["transport_operation"])
            ),
            transport_failure_class=(
                None
                if row["transport_failure_class"] is None
                else str(row["transport_failure_class"])
            ),
            transport_status_code=(
                None if row["transport_status_code"] is None else int(row["transport_status_code"])
            ),
            transport_retry_after=(
                None if row["transport_retry_after"] is None else int(row["transport_retry_after"])
            ),
            transport_consecutive_failures=int(row["transport_consecutive_failures"]),
            transport_success_at=(
                None if row["transport_success_at"] is None else str(row["transport_success_at"])
            ),
            updated_at=str(row["updated_at"]),
        )

    def upsert_runtime_health(
        self,
        *,
        component: str,
        instance_id: str,
        pid: int,
        process_start_marker: str,
        started_at: datetime,
        heartbeat_at: datetime,
        runtime: str | None = None,
        agent_id: str | None = None,
        success_at: datetime | None = None,
        error_code: str | None = None,
        activity_state: str | None = None,
        active_job_id: str | None = None,
        active_lease_expires_at: datetime | None = None,
        provider_state: str = "unknown",
        quota_remaining_percent: float | None = None,
        quota_reset_at: datetime | None = None,
        release_identity: ReleaseIdentity = CURRENT_RELEASE,
        transport_operation: str | None = None,
        transport_failure_class: str | None = None,
        transport_status_code: int | None = None,
        transport_retry_after: int | None = None,
        transport_consecutive_failures: int = 0,
        transport_success_at: datetime | None = None,
    ) -> RuntimeHealthRecord:
        if component not in {
            "controller",
            "sender",
            "monitor",
            "provider_worker",
            "project_provisioner",
        }:
            raise self._state_error("invalid runtime health component")
        instance_id = self._bounded(instance_id, name="instance id", maximum=128)
        process_start_marker = self._bounded(
            process_start_marker, name="process start marker", maximum=128
        )
        runtime = self._optional_bounded(runtime, name="runtime", maximum=64)
        agent_id = self._optional_bounded(agent_id, name="agent id", maximum=64)
        error_code = self._optional_bounded(error_code, name="error code", maximum=128)
        active_job_id = self._optional_bounded(active_job_id, name="active job id", maximum=128)
        if pid <= 0:
            raise self._state_error("invalid runtime health pid")
        if component == "provider_worker":
            if runtime is None or agent_id is None:
                raise self._state_error("provider worker health requires runtime and agent id")
        if provider_state not in {"unknown", "ready", "limited", "exhausted", "unavailable"}:
            raise self._state_error("invalid provider state")
        if component != "provider_worker" and provider_state != "unknown":
            raise self._state_error("only provider worker health may report provider state")
        if quota_remaining_percent is not None and not 0 <= quota_remaining_percent <= 100:
            raise self._state_error("invalid quota remaining percent")
        if component != "provider_worker" and (
            quota_remaining_percent is not None or quota_reset_at is not None
        ):
            raise self._state_error("only provider worker health may report quota state")
        activity_state = activity_state or ("leased" if active_job_id is not None else "idle")
        if activity_state not in {"idle", "leased", "executing", "sending", "unknown"}:
            raise self._state_error("invalid runtime activity state")
        if active_job_id is None and activity_state not in {"idle", "unknown"}:
            raise self._state_error("active activity state requires a job id")
        if active_job_id is None and active_lease_expires_at is not None:
            raise self._state_error("active lease requires a job id")
        started = self._timestamp(started_at)
        heartbeat = self._timestamp(heartbeat_at)
        success = None if success_at is None else self._timestamp(success_at)
        lease_expires = (
            None if active_lease_expires_at is None else self._timestamp(active_lease_expires_at)
        )
        quota_reset = None if quota_reset_at is None else self._timestamp(quota_reset_at)
        release_version = self._bounded(
            release_identity.package_version, name="release version", maximum=64
        )
        release_git_sha = release_identity.git_sha
        if (
            release_git_sha is not None
            and re.fullmatch(r"[0-9a-f]{40,64}", release_git_sha) is None
        ):
            raise self._state_error("invalid release Git SHA")
        release_built_at = release_identity.built_at
        if release_built_at is not None:
            self._parse_timestamp(release_built_at, name="release build time")
        transport_operation = self._optional_bounded(
            transport_operation, name="transport operation", maximum=32
        )
        transport_failure_class = self._optional_bounded(
            transport_failure_class, name="transport failure class", maximum=64
        )
        if (
            transport_operation is not None
            and re.fullmatch(r"[a-z_]+", transport_operation) is None
        ):
            raise self._state_error("invalid transport operation")
        if (
            transport_failure_class is not None
            and re.fullmatch(r"[a-z_]+", transport_failure_class) is None
        ):
            raise self._state_error("invalid transport failure class")
        if transport_status_code is not None and (
            isinstance(transport_status_code, bool) or not 100 <= transport_status_code <= 599
        ):
            raise self._state_error("invalid transport status code")
        if transport_retry_after is not None and (
            isinstance(transport_retry_after, bool) or not 0 <= transport_retry_after <= 86_400
        ):
            raise self._state_error("invalid transport retry-after")
        if isinstance(transport_consecutive_failures, bool) or not (
            0 <= transport_consecutive_failures <= 1_000_000
        ):
            raise self._state_error("invalid transport consecutive failures")
        if transport_consecutive_failures == 0 and any(
            value is not None
            for value in (
                transport_operation,
                transport_failure_class,
                transport_status_code,
                transport_retry_after,
            )
        ):
            raise self._state_error("transport failure detail requires a positive failure count")
        if transport_consecutive_failures > 0 and (
            transport_operation is None or transport_failure_class is None
        ):
            raise self._state_error("transport failures require operation and failure class")
        if transport_status_code is not None and transport_failure_class is None:
            raise self._state_error("transport status requires a failure class")
        if transport_retry_after is not None and transport_failure_class is None:
            raise self._state_error("transport retry-after requires a failure class")
        transport_success = (
            None if transport_success_at is None else self._timestamp(transport_success_at)
        )
        with self._write_transaction():
            self._connection.execute(
                """INSERT INTO runtime_health (
                       component, instance_id, runtime, agent_id, pid, process_start_marker,
                       started_at, heartbeat_at, success_at, error_code, activity_state,
                       active_job_id, active_lease_expires_at, provider_state,
                       quota_remaining_percent, quota_reset_at, release_version,
                       release_git_sha, release_built_at, release_clean,
                       transport_operation, transport_failure_class, transport_status_code,
                       transport_retry_after, transport_consecutive_failures,
                       transport_success_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(component, instance_id) DO UPDATE SET
                     runtime = excluded.runtime,
                     agent_id = excluded.agent_id,
                     pid = excluded.pid,
                     process_start_marker = excluded.process_start_marker,
                     started_at = CASE
                       WHEN runtime_health.process_start_marker = excluded.process_start_marker
                       THEN runtime_health.started_at ELSE excluded.started_at END,
                     heartbeat_at = excluded.heartbeat_at,
                     success_at = excluded.success_at,
                     error_code = excluded.error_code,
                     activity_state = excluded.activity_state,
                     active_job_id = excluded.active_job_id,
                     active_lease_expires_at = excluded.active_lease_expires_at,
                     provider_state = excluded.provider_state,
                     quota_remaining_percent = excluded.quota_remaining_percent,
                     quota_reset_at = excluded.quota_reset_at,
                     release_version = excluded.release_version,
                     release_git_sha = excluded.release_git_sha,
                     release_built_at = excluded.release_built_at,
                     release_clean = excluded.release_clean,
                     transport_operation = excluded.transport_operation,
                     transport_failure_class = excluded.transport_failure_class,
                     transport_status_code = excluded.transport_status_code,
                     transport_retry_after = excluded.transport_retry_after,
                     transport_consecutive_failures = excluded.transport_consecutive_failures,
                     transport_success_at = excluded.transport_success_at,
                     updated_at = excluded.updated_at""",
                (
                    component,
                    instance_id,
                    runtime,
                    agent_id,
                    pid,
                    process_start_marker,
                    started,
                    heartbeat,
                    success,
                    error_code,
                    activity_state,
                    active_job_id,
                    lease_expires,
                    provider_state,
                    quota_remaining_percent,
                    quota_reset,
                    release_version,
                    release_git_sha,
                    release_built_at,
                    int(release_identity.clean_tree),
                    transport_operation,
                    transport_failure_class,
                    transport_status_code,
                    transport_retry_after,
                    transport_consecutive_failures,
                    transport_success,
                    heartbeat,
                ),
            )
        record = self.get_runtime_health(component, instance_id)
        if record is None:
            raise self._state_error("failed to persist runtime health")
        return record

    def get_runtime_health(self, component: str, instance_id: str) -> RuntimeHealthRecord | None:
        row = self._connection.execute(
            "SELECT * FROM runtime_health WHERE component = ? AND instance_id = ?",
            (component, instance_id),
        ).fetchone()
        return None if row is None else self.record(row)

    def list_runtime_health(self) -> tuple[RuntimeHealthRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM runtime_health ORDER BY component, instance_id"
        ).fetchall()
        return tuple(self.record(row) for row in rows)

    def runtime_health_status(
        self,
        component: str,
        instance_id: str,
        *,
        now: datetime | None = None,
        degraded_after: timedelta = timedelta(seconds=60),
        stale_after: timedelta = timedelta(minutes=3),
    ) -> RuntimeHealthStatus:
        if degraded_after.total_seconds() <= 0 or stale_after <= degraded_after:
            raise self._state_error("invalid runtime health staleness thresholds")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise self._state_error("runtime health classification time must be timezone-aware")
        record = self.get_runtime_health(component, instance_id)
        if record is None:
            return RuntimeHealthStatus("unknown", None)
        heartbeat = self._parse_timestamp(record.heartbeat_at, name="runtime heartbeat")
        age = current.astimezone(timezone.utc) - heartbeat
        if age > stale_after:
            status = "stale"
        elif age > degraded_after:
            status = "degraded"
        elif (
            record.error_code is not None
            or record.transport_consecutive_failures >= TELEGRAM_HEALTH_FAILURE_THRESHOLD
            or record.provider_state in {"limited", "exhausted", "unavailable"}
        ):
            status = "degraded"
        else:
            status = "healthy"
        return RuntimeHealthStatus(status, record)


__all__ = [
    "RuntimeHealthRecord",
    "RuntimeHealthStateFacade",
    "RuntimeHealthStatus",
]
