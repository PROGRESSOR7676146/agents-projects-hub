from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class IndeterminateAuditError(RuntimeError):
    pass


_ACTIONS = {
    "persisted_result": "review_invariant_before_delivery",
    "completed_checkpoint": "review_for_result_recovery",
    "partial_checkpoint": "review_partial_then_continue",
    "accepted_without_visible_result": "inspect_provider_and_project",
    "thread_without_accepted_turn": "inspect_provider_and_project",
    "no_execution_checkpoint": "retain_unknown_without_replay",
}


def _evidence(row: sqlite3.Row) -> str:
    if row["result_present"]:
        return "persisted_result"
    if row["completed_text_present"]:
        return "completed_checkpoint"
    if int(row["visible_item_count"]):
        return "partial_checkpoint"
    if row["provider_turn_id"]:
        return "accepted_without_visible_result"
    if row["provider_thread_id"]:
        return "thread_without_accepted_turn"
    return "no_execution_checkpoint"


def classify_indeterminate_jobs(state_path: Path) -> dict[str, Any]:
    """Classify terminal uncertain work from local evidence without mutating or invoking it."""
    state_path = state_path.expanduser().resolve(strict=True)
    if not state_path.is_file():
        raise IndeterminateAuditError("state path is not a regular file")
    connection = sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "provider_jobs" not in tables:
            raise IndeterminateAuditError("state database has no provider job queue")
        checkpoint_columns = (
            "checkpoint.provider_thread_id, checkpoint.provider_turn_id, "
            "CASE WHEN length(checkpoint.completed_text) > 0 THEN 1 ELSE 0 END "
            "AS completed_text_present"
            if "provider_execution_checkpoints" in tables
            else "NULL AS provider_thread_id, NULL AS provider_turn_id, 0 AS completed_text_present"
        )
        checkpoint_join = (
            "LEFT JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id = jobs.job_id"
            if "provider_execution_checkpoints" in tables
            else ""
        )
        visible_columns = (
            "COALESCE(visible.item_count, 0) AS visible_item_count"
            if "provider_visible_items" in tables
            else "0 AS visible_item_count"
        )
        visible_join = (
            "LEFT JOIN (SELECT job_id, COUNT(*) AS item_count FROM provider_visible_items "
            "GROUP BY job_id) visible ON visible.job_id = jobs.job_id"
            if "provider_visible_items" in tables
            else ""
        )
        result_columns = (
            "CASE WHEN result.job_id IS NULL THEN 0 ELSE 1 END AS result_present"
            if "provider_job_results" in tables
            else "0 AS result_present"
        )
        result_join = (
            "LEFT JOIN provider_job_results result ON result.job_id = jobs.job_id"
            if "provider_job_results" in tables
            else ""
        )
        notice_columns = (
            "COALESCE(outbox.status, 'missing') AS notice_status"
            if "telegram_outbox" in tables
            else "'missing' AS notice_status"
        )
        notice_join = (
            "LEFT JOIN telegram_outbox outbox ON outbox.job_id = jobs.job_id"
            if "telegram_outbox" in tables
            else ""
        )
        rows = connection.execute(
            f"""SELECT jobs.job_id, jobs.agent_id, jobs.created_at, jobs.updated_at,
                       jobs.error_class, jobs.error_code,
                       {checkpoint_columns}, {visible_columns}, {result_columns},
                       {notice_columns}
                FROM provider_jobs jobs
                {checkpoint_join}
                {visible_join}
                {result_join}
                {notice_join}
                WHERE jobs.status = 'indeterminate'
                ORDER BY jobs.created_at, jobs.job_id"""
        ).fetchall()
    finally:
        connection.close()

    evidence_counts: Counter[str] = Counter()
    notice_counts: Counter[str] = Counter()
    records: list[dict[str, object]] = []
    for row in rows:
        evidence = _evidence(row)
        notice_status = str(row["notice_status"])
        evidence_counts[evidence] += 1
        notice_counts[notice_status] += 1
        records.append(
            {
                "job_id": str(row["job_id"]),
                "agent_id": str(row["agent_id"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "error_class": row["error_class"],
                "error_code": row["error_code"],
                "evidence": evidence,
                "notice_status": notice_status,
                "recommended_action": _ACTIONS[evidence],
            }
        )
    return {
        "schema_version": schema_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(records),
        "evidence": dict(sorted(evidence_counts.items())),
        "notice_status": dict(sorted(notice_counts.items())),
        "productive_replay_authorized": False,
        "records": records,
    }


def write_private_indeterminate_report(destination: Path, report: dict[str, Any]) -> None:
    """Create one private report without overwriting an earlier audit artifact."""
    destination = destination.expanduser()
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
