"""Bounded passive projection of existing outcome evidence, never assessment authority."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

REFERENCE_LIMIT = 64

_PROJECTION = """
WITH target AS (
    SELECT j.job_id, j.topic_id, j.session_id, j.session_generation,
           j.agent_id, j.model, j.effort, j.status, j.attempt_count,
           j.created_at AS admitted_at, j.provider_started_at AS worker_started_at,
           r.result_id, r.actual_model AS stored_model, r.created_at AS result_at,
           c.job_id IS NOT NULL AS checkpoint_present,
           c.completed_text IS NOT NULL AS completion_saved,
           c.provider_turn_id IS NOT NULL AS native_turn_id_saved,
           e.terminal_status, e.observed_at AS terminal_at,
           h.resolution, h.resolved_at,
           o.outbox_id, o.status AS delivery_status, o.delivered_at,
           (SELECT COUNT(*) FROM provider_visible_items v WHERE v.job_id=j.job_id)
               AS visible_item_count
    FROM provider_jobs j
    JOIN topics topic ON topic.topic_id=j.topic_id
    LEFT JOIN provider_job_results r ON r.job_id=j.job_id
    LEFT JOIN provider_execution_checkpoints c ON c.job_id=j.job_id
    LEFT JOIN provider_turn_terminal_evidence e ON e.job_id=j.job_id
    LEFT JOIN provider_job_resolutions h ON h.job_id=j.job_id
    LEFT JOIN telegram_outbox o ON o.job_id=j.job_id AND r.result_id IS NOT NULL
                                  AND o.sender_agent_id=j.agent_id
                                  AND o.chat_id=j.chat_id AND o.thread_id=topic.thread_id
    WHERE j.job_id=?
), artifacts AS (
    SELECT p.outbox_id, p.part_index, p.file_size, p.file_sha256,
           p.telegram_message_id > 0 AS receipt_present
    FROM telegram_outbox_parts p JOIN target t ON t.outbox_id=p.outbox_id
    WHERE p.part_type='document'
), lineage AS (
    SELECT 'absorbed_into' AS kind, a.parent_job_id AS job_id
        FROM provider_job_absorptions a JOIN target t ON a.child_job_id=t.job_id
    UNION ALL SELECT 'absorbs', a.child_job_id
        FROM provider_job_absorptions a JOIN target t ON a.parent_job_id=t.job_id
    UNION ALL SELECT 'continued_from', c.source_job_id
        FROM provider_job_continuations c JOIN target t ON c.continuation_job_id=t.job_id
    UNION ALL SELECT 'continued_as', c.continuation_job_id
        FROM provider_job_continuations c JOIN target t ON c.source_job_id=t.job_id
    UNION ALL SELECT 'retry_of', r.source_job_id
        FROM provider_preexecution_retries r JOIN target t ON r.child_job_id=t.job_id
    UNION ALL SELECT 'retried_as', r.child_job_id
        FROM provider_preexecution_retries r JOIN target t ON r.source_job_id=t.job_id
)
SELECT t.*, (SELECT user_version FROM pragma_user_version) AS observed_schema,
    (SELECT COUNT(*) FROM telegram_outbox_parts p WHERE p.outbox_id=t.outbox_id)
        AS part_count,
    (SELECT COUNT(*) FROM telegram_outbox_parts p WHERE p.outbox_id=t.outbox_id
        AND p.telegram_message_id>0) AS receipt_count,
    (SELECT COUNT(*) FROM artifacts) AS artifact_count,
    (SELECT json_group_array(json_object(
        'outbox_id', outbox_id, 'part_index', part_index, 'size', file_size,
        'sha256', file_sha256, 'receipt_present', receipt_present))
        FROM (SELECT * FROM artifacts ORDER BY part_index LIMIT ?)) AS artifact_json,
    (SELECT COUNT(*) FROM lineage) AS lineage_count,
    (SELECT json_group_array(json_object('kind', kind, 'job_id', job_id))
        FROM (SELECT * FROM lineage ORDER BY kind, job_id LIMIT ?)) AS lineage_json
FROM target t
"""


@dataclass(frozen=True, slots=True)
class ProviderJobOutcome:
    """Allowlisted local diagnostics; carries no owner-decision authority."""

    _projection: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._projection)


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _interval(
    start: str | None, end: str | None, start_source: str, end_source: str
) -> dict[str, Any]:
    first, last = _time(start), _time(end)
    seconds = (last - first).total_seconds() if first is not None and last is not None else None
    unknown_reason = "missing_or_invalid_endpoints" if seconds is None else None
    if seconds is not None and seconds < 0:
        seconds = None
        unknown_reason = "reversed_endpoints"
    return {
        "start": first.isoformat() if first else None,
        "end": last.isoformat() if last else None,
        "start_source": start_source,
        "end_source": end_source,
        "seconds": seconds,
        "unknown_reason": unknown_reason,
    }


def _timestamp(value: str | None) -> str | None:
    parsed = _time(value)
    return parsed.isoformat() if parsed is not None else None


def _page(raw: str, total: int, keys: set[str], order: tuple[str, ...]) -> dict[str, Any]:
    items = json.loads(raw)
    if (
        not isinstance(items, list)
        or len(items) > REFERENCE_LIMIT
        or any(not isinstance(item, dict) or set(item) != keys for item in items)
    ):
        raise ValueError("invalid outcome reference projection")
    items.sort(key=lambda item: tuple(item[key] for key in order))
    return {"items": items, "total": total, "truncated": total > len(items)}


class OutcomeJournalStateFacade:
    """One read statement on the HubState-owned connection; no new transactions."""

    def __init__(
        self, connection: sqlite3.Connection, state_error: type[Exception], expected_schema: int
    ) -> None:
        self._connection = connection
        self._state_error = state_error
        self._expected_schema = expected_schema

    def read(self, job_id: str) -> ProviderJobOutcome:
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", job_id):
            raise self._state_error("outcome_job_id_invalid")
        try:
            row = self._connection.execute(
                _PROJECTION, (job_id, REFERENCE_LIMIT, REFERENCE_LIMIT)
            ).fetchone()
        except sqlite3.Error as error:
            raise self._state_error("outcome_projection_unavailable") from error
        if row is None:
            raise self._state_error("outcome_job_not_found")
        if row["observed_schema"] != self._expected_schema:
            raise self._state_error("state_schema_unsupported")
        try:
            return self._project(row)
        except (sqlite3.Error, ValueError, TypeError, OverflowError) as error:
            raise self._state_error("outcome_projection_unavailable") from error

    @staticmethod
    def _project(row: sqlite3.Row) -> ProviderJobOutcome:
        artifacts = _page(
            row["artifact_json"],
            row["artifact_count"],
            {"outbox_id", "part_index", "size", "sha256", "receipt_present"},
            ("part_index",),
        )
        for artifact in artifacts["items"]:
            artifact["receipt_present"] = bool(artifact["receipt_present"])
        delivery = None
        delivery_time = None
        if row["outbox_id"] is not None:
            receipts_complete = row["part_count"] > 0 and row["part_count"] == row["receipt_count"]
            delivered = row["delivery_status"] == "delivered" and receipts_complete
            delivery_time = row["delivered_at"] if delivered else None
            delivery = {
                "outbox_id": row["outbox_id"],
                "status": row["delivery_status"],
                "parts_total": row["part_count"],
                "parts_receipted": row["receipt_count"],
                "receipts_complete": receipts_complete,
            }
        return ProviderJobOutcome(
            {
                "format_version": 1,
                "diagnostic_only": True,
                "productive_replay_authorized": False,
                "task": {
                    "job_id": row["job_id"],
                    "topic_id": row["topic_id"],
                    "session_id": row["session_id"],
                    "session_generation": row["session_generation"],
                    "status": row["status"],
                    "attempt_count": row["attempt_count"],
                },
                "participant": {
                    "agent_id": row["agent_id"],
                    "runtime": None,
                    "requested_model": row["model"],
                    "requested_effort": row["effort"],
                    "stored_model_label": row["stored_model"],
                    "stored_model_source": "provider_job_results.actual_model",
                    "stored_model_provenance": "may_include_requested_fallback",
                    "observed_model": None,
                    "observed_effort": None,
                    "unknown_reason": "not_recorded_with_observation_source",
                },
                "result": {"result_id": row["result_id"], "job_id": row["job_id"]}
                if row["result_id"] is not None
                else None,
                "result_delivery": delivery,
                "inconsistencies": ["result_delivery_missing_or_mismatched"]
                if row["result_id"] is not None and delivery is None
                else [],
                "execution": {
                    "checkpoint_present": bool(row["checkpoint_present"]),
                    "completion_saved": bool(row["completion_saved"]),
                    "native_turn_id_saved": bool(row["native_turn_id_saved"]),
                    "visible_item_count": row["visible_item_count"],
                    "terminal_status": row["terminal_status"],
                    "terminal_observation": _timestamp(row["terminal_at"]),
                    "historical_resolution": row["resolution"],
                    "resolution_observation": _timestamp(row["resolved_at"]),
                },
                "artifacts": artifacts,
                "lineage": _page(
                    row["lineage_json"],
                    row["lineage_count"],
                    {"kind", "job_id"},
                    ("kind", "job_id"),
                ),
                "acceptance": {
                    "decision": "unknown",
                    "reason": "no_authoritative_owner_decision_recorded",
                },
                "usage": {
                    "tokens": None,
                    "monetary_cost": None,
                    "unknown_reason": "per_job_usage_not_recorded",
                },
                "timing": {
                    "admission_to_result_commit": _interval(
                        row["admitted_at"],
                        row["result_at"],
                        "provider_jobs.created_at",
                        "provider_job_results.created_at",
                    ),
                    "latest_worker_phase_to_result_commit": _interval(
                        row["worker_started_at"],
                        row["result_at"],
                        "provider_jobs.provider_started_at",
                        "provider_job_results.created_at",
                    ),
                    "result_commit_to_delivery_receipt": _interval(
                        row["result_at"],
                        delivery_time,
                        "provider_job_results.created_at",
                        "telegram_outbox.delivered_at",
                    ),
                    "native_execution_seconds": None,
                    "queue_wait_seconds": None,
                    "approval_wait_seconds": None,
                },
            }
        )
