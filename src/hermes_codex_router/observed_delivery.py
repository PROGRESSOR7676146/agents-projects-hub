"""Preserve uncertain delivery while recording independently observed terminality."""

from __future__ import annotations

import sqlite3


def archive_recovery_notice(
    connection: sqlite3.Connection, *, job_id: str, outbox_id: str, timestamp: str
) -> None:
    """Snapshot the replaced notice and every part within the caller's transaction."""
    if not connection.in_transaction:
        raise RuntimeError("notice archival requires an existing transaction")
    connection.execute(
        """INSERT INTO provider_recovery_notices
           (job_id, outbox_id, telegram_html, delivery_status, telegram_message_id, saved_at)
           SELECT job_id, outbox_id, telegram_html, status, telegram_message_id, ?
           FROM telegram_outbox WHERE job_id=? AND outbox_id=?""",
        (timestamp, job_id, outbox_id),
    )
    connection.execute(
        """INSERT INTO provider_recovery_notice_parts
           (job_id,outbox_id,part_index,telegram_html,telegram_message_id,delivered_at,
            part_type,file_path,file_name,file_size,file_sha256,receipt_validation_version)
           SELECT ?,outbox_id,part_index,telegram_html,telegram_message_id,delivered_at,
                  part_type,file_path,file_name,file_size,file_sha256,receipt_validation_version
           FROM telegram_outbox_parts WHERE outbox_id=?""",
        (job_id, outbox_id),
    )


def retain_delivery_terminal_evidence(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    status: str,
    thread_id: str,
    turn_id: str,
    root: str,
    timestamp: str,
    completed_text: str | None,
) -> None:
    """Caller owns transaction and has validated exact job/session/root/native binding."""
    if not connection.in_transaction:
        raise RuntimeError("terminal observation requires an existing transaction")
    if status not in {"completed", "failed", "interrupted"}:
        raise ValueError("terminal observation status invalid")
    if status == "completed":
        if completed_text is None or len(completed_text) > 200_000:
            raise ValueError("terminal completion is missing or exceeds bound")
        row = connection.execute(
            "SELECT completed_text FROM provider_execution_checkpoints WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None or (row[0] is not None and row[0] != completed_text):
            raise ValueError("terminal completion conflicts with saved checkpoint")
        connection.execute(
            "UPDATE provider_execution_checkpoints SET completed_text=?, updated_at=? WHERE job_id=?",
            (completed_text, timestamp, job_id),
        )
    connection.execute(
        """INSERT INTO provider_turn_terminal_evidence
           (job_id,terminal_status,provider_thread_id,provider_turn_id,project_root,observed_at)
           VALUES (?,?,?,?,?,?)""",
        (job_id, status, thread_id, turn_id, root, timestamp),
    )
