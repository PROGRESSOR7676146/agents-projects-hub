"""Preserve uncertain delivery while recording independently observed terminality."""

from __future__ import annotations

import sqlite3


def _append_declared_loss_archive_tombstones(
    connection: sqlite3.Connection, *, outbox_id: str
) -> None:
    """Bind every declared-loss part to the exact archive before source deletion."""
    expected = connection.execute(
        "SELECT COUNT(*) FROM artifact_loss_items WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0]
    if not expected:
        return
    archived = connection.execute(
        """SELECT COUNT(*)
           FROM artifact_loss_items item
           JOIN telegram_outbox outbox ON outbox.outbox_id=item.outbox_id
           JOIN telegram_outbox_parts part
             ON part.outbox_id=outbox.outbox_id AND part.part_index=item.part_index
           JOIN provider_recovery_notices notice
             ON notice.job_id=outbox.job_id AND notice.outbox_id=outbox.outbox_id
           JOIN provider_recovery_notice_parts archived
             ON archived.job_id=notice.job_id AND archived.outbox_id=outbox.outbox_id
            AND archived.part_index=part.part_index
           WHERE item.outbox_id=?
             AND part.part_type='document'
             AND part.file_sha256=item.file_sha256 AND part.file_size=item.file_size
             AND outbox.status IN ('delivered','unknown','failed')
             AND outbox.lease_owner IS NULL AND outbox.lease_token IS NULL
             AND outbox.lease_expires_at IS NULL
             AND (outbox.status!='failed' OR outbox.attempt_count=20)
             AND notice.telegram_html IS outbox.telegram_html
             AND notice.delivery_status=outbox.status
             AND notice.telegram_message_id IS outbox.telegram_message_id
             AND archived.telegram_html IS part.telegram_html
             AND archived.telegram_message_id IS part.telegram_message_id
             AND archived.delivered_at IS part.delivered_at
             AND archived.part_type=part.part_type
             AND archived.file_path IS part.file_path
             AND archived.file_name IS part.file_name
             AND archived.file_size IS part.file_size
             AND archived.file_sha256 IS part.file_sha256
             AND archived.receipt_validation_version=part.receipt_validation_version""",
        (outbox_id,),
    ).fetchone()[0]
    if archived != expected:
        raise RuntimeError("declared-loss archive lacks an exact copy of every source part")
    connection.execute(
        """INSERT INTO artifact_loss_archive_tombstones (
               operation_id,outbox_id,part_index,source_binding_hash,file_sha256,file_size,
               source_job_id,source_sender_agent_id,source_chat_id,source_thread_id,
               source_purpose,source_telegram_html,source_status,source_attempt_count,
               source_telegram_message_id,source_delivered_at,source_part_telegram_html,
               source_part_type,source_file_path,source_file_name,
               source_part_telegram_message_id,source_part_delivered_at,
               source_part_receipt_validation_version,source_session_id,
               source_session_generation,source_topic_id,source_topic_sequence,source_agent_id,
               source_project_id,source_topic_chat_id,source_topic_thread_id,archive_job_id,
               archive_outbox_id,archive_part_index,archive_saved_at
           )
           SELECT item.operation_id,item.outbox_id,item.part_index,item.source_binding_hash,
                  item.file_sha256,item.file_size,outbox.job_id,outbox.sender_agent_id,
                  outbox.chat_id,outbox.thread_id,outbox.purpose,outbox.telegram_html,
                  outbox.status,outbox.attempt_count,outbox.telegram_message_id,
                  outbox.delivered_at,part.telegram_html,part.part_type,part.file_path,
                  part.file_name,part.telegram_message_id,part.delivered_at,
                  part.receipt_validation_version,job.session_id,job.session_generation,
                  job.topic_id,job.topic_sequence,job.agent_id,topic.project_id,topic.chat_id,
                  topic.thread_id,notice.job_id,notice.outbox_id,archived.part_index,notice.saved_at
           FROM artifact_loss_items item
           JOIN telegram_outbox outbox ON outbox.outbox_id=item.outbox_id
           JOIN telegram_outbox_parts part
             ON part.outbox_id=outbox.outbox_id AND part.part_index=item.part_index
           JOIN provider_jobs job ON job.job_id=outbox.job_id
           JOIN topics topic ON topic.topic_id=job.topic_id
           JOIN provider_recovery_notices notice
             ON notice.job_id=outbox.job_id AND notice.outbox_id=outbox.outbox_id
           JOIN provider_recovery_notice_parts archived
             ON archived.job_id=notice.job_id AND archived.outbox_id=outbox.outbox_id
            AND archived.part_index=part.part_index
           WHERE item.outbox_id=?""",
        (outbox_id,),
    )
    actual = connection.execute(
        "SELECT COUNT(*) FROM artifact_loss_archive_tombstones WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0]
    if actual != expected:
        raise RuntimeError("declared-loss archive did not retain every source part")


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
    _append_declared_loss_archive_tombstones(connection, outbox_id=outbox_id)


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
