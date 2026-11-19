"""Evidence from one exact send, separate from authority to start another send.

HubState supplies the connection and owns every transaction. Detached attempt
evidence deliberately survives the existing notice archival/replacement path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable


class DeliveryReceipts:
    def __init__(self, db: sqlite3.Connection, state_error: Callable[[str], Exception]) -> None:
        self.db = db
        self.error = state_error
        self._cursors: dict[tuple[str, tuple[str, ...]], int] = {}

    def _require_transaction(self) -> None:
        if not self.db.in_transaction:
            raise self.error("receipt evidence requires an owning transaction")

    def _binding(self, kind: str, target: str, part: int) -> str | None:
        if kind == "final":
            row = self.db.execute(
                """SELECT o.outbox_id,o.job_id,o.sender_agent_id,o.chat_id,o.thread_id,o.purpose,
                   o.telegram_html,p.part_index,p.telegram_html AS part_html,p.part_type,
                   p.file_path,p.file_name,p.file_size,p.file_sha256,
                   j.session_id,j.session_generation,j.topic_id,j.topic_sequence,j.agent_id,
                   t.project_id,t.chat_id AS topic_chat,t.thread_id AS topic_thread
                   FROM telegram_outbox o JOIN telegram_outbox_parts p USING(outbox_id)
                   JOIN provider_jobs j ON j.job_id=o.job_id JOIN topics t USING(topic_id)
                   WHERE o.outbox_id=? AND p.part_index=?""",
                (target, part),
            ).fetchone()
        else:
            row = self.db.execute(
                """SELECT p.progress_id,p.item_sequence,p.job_id,p.sender_agent_id,p.chat_id,
                   p.thread_id,p.telegram_html,j.session_id,j.session_generation,j.topic_id,
                   j.topic_sequence,j.agent_id,t.project_id,t.chat_id AS topic_chat,
                   t.thread_id AS topic_thread FROM provider_progress_deliveries p
                   JOIN provider_jobs j USING(job_id) JOIN topics t USING(topic_id)
                   WHERE p.progress_id=?""",
                (target,),
            ).fetchone()
        if row is None:
            return None
        data = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(data.encode()).hexdigest()

    def artifact_retirement_snapshot(
        self, outbox_id: str, part_index: int
    ) -> dict[str, object] | None:
        """Return one terminal document's independently revalidated receipt snapshot.

        This deliberately derives the snapshot from both mutable delivery
        projection and immutable send evidence.  It is usable during the
        schema migration as well as the live terminal-delivery transaction;
        incomplete historical rows stay absent rather than being inferred.
        """
        part = self.db.execute(
            """SELECT outbox.delivered_at AS terminal_delivered_at,
                      parts.outbox_id,parts.part_index,parts.telegram_message_id,
                      parts.delivered_at,parts.file_path,parts.file_name,parts.file_size,
                      parts.file_sha256
               FROM telegram_outbox outbox
               JOIN telegram_outbox_parts parts USING(outbox_id)
               WHERE outbox.outbox_id=? AND outbox.status='delivered'
                 AND outbox.delivered_at IS NOT NULL AND parts.part_index=?
                 AND parts.part_type='document' AND parts.receipt_validation_version=1
                 AND parts.telegram_message_id IS NOT NULL AND parts.delivered_at IS NOT NULL""",
            (outbox_id, part_index),
        ).fetchone()
        if part is None:
            return None
        attempts = self.db.execute(
            """SELECT attempt.attempt_id,attempt.binding_hash
               FROM telegram_send_attempts attempt
               JOIN telegram_send_receipts receipt USING(attempt_id)
               WHERE attempt.target_kind='final' AND attempt.target_id=?
                 AND attempt.part_index=?
                 AND receipt.telegram_message_id=? AND receipt.recorded_at=?
                 AND NOT EXISTS (
                     SELECT 1 FROM telegram_send_attempts newer
                     WHERE newer.target_kind='final' AND newer.target_id=attempt.target_id
                       AND newer.part_index=attempt.part_index
                       AND newer.sequence>attempt.sequence
                 )
               ORDER BY attempt.sequence""",
            (
                outbox_id,
                part_index,
                int(part["telegram_message_id"]),
                str(part["delivered_at"]),
            ),
        ).fetchall()
        if len(attempts) != 1:
            return None
        attempt = attempts[0]
        if self._binding("final", outbox_id, part_index) != attempt["binding_hash"]:
            return None
        return {
            "outbox_id": outbox_id,
            "part_index": part_index,
            "attempt_id": str(attempt["attempt_id"]),
            "binding_hash": str(attempt["binding_hash"]),
            "telegram_message_id": int(part["telegram_message_id"]),
            "delivered_at": str(part["delivered_at"]),
            "file_path": str(part["file_path"]),
            "file_name": str(part["file_name"]),
            "file_size": int(part["file_size"]),
            "file_sha256": str(part["file_sha256"]),
            # The terminal outbox receipt is the only historical time that
            # proves this aggregate record existed; do not invent a cleanup
            # filesystem timestamp for a pre-schema artifact.
            "retired_at": str(part["terminal_delivered_at"]),
        }

    def insert_artifact_retirement(self, snapshot: dict[str, object]) -> None:
        """Append one already revalidated document retirement inside this transaction."""
        self._require_transaction()
        self.db.execute(
            """INSERT INTO telegram_artifact_retirements
               (outbox_id,part_index,attempt_id,binding_hash,telegram_message_id,delivered_at,
                file_path,file_name,file_size,file_sha256,retired_at)
               VALUES (:outbox_id,:part_index,:attempt_id,:binding_hash,:telegram_message_id,
                       :delivered_at,:file_path,:file_name,:file_size,:file_sha256,:retired_at)""",
            snapshot,
        )

    def retire_terminal_artifacts(self, outbox_id: str) -> None:
        """Append proof for every document only once the entire outbox is terminal."""
        self._require_transaction()
        parts = self.db.execute(
            """SELECT part_index FROM telegram_outbox_parts
               WHERE outbox_id=? AND part_type='document' ORDER BY part_index""",
            (outbox_id,),
        ).fetchall()
        for part in parts:
            part_index = int(part["part_index"])
            snapshot = self.artifact_retirement_snapshot(outbox_id, part_index)
            if snapshot is None:
                raise self.error("terminal document lacks exact receipt evidence")
            self.insert_artifact_retirement(snapshot)

    def begin(self, kind: str, target: str, token: str, part: int, timestamp: str) -> None:
        self._require_transaction()
        binding = self._binding(kind, target, part)
        if binding is None:
            raise self.error("send target disappeared")
        self.db.execute(
            """INSERT INTO telegram_send_attempts
               (attempt_id,target_kind,target_id,part_index,binding_hash,started_at)
               VALUES(?,?,?,?,?,?)""",
            (token, kind, target, part, binding, timestamp),
        )

    def record(
        self, kind: str, target: str, token: str, part: int | None, message_id: int, timestamp: str
    ) -> None:
        self._require_transaction()
        if type(message_id) is not int or message_id <= 0:
            raise self.error("invalid Telegram message id")
        attempt = self.db.execute(
            "SELECT * FROM telegram_send_attempts WHERE attempt_id=?",
            (token,),
        ).fetchone()
        if (
            attempt is None
            or attempt["target_kind"] != kind
            or attempt["target_id"] != target
            or (part is not None and attempt["part_index"] != part)
        ):
            raise self.error("receipt does not match the exact send attempt")
        old = self.db.execute(
            "SELECT telegram_message_id FROM telegram_send_receipts WHERE attempt_id=?",
            (token,),
        ).fetchone()
        if old is not None:
            if old[0] != message_id:
                raise self.error("conflicting Telegram receipt")
            return
        self.db.execute(
            "INSERT INTO telegram_send_receipts VALUES(?,?,?)",
            (token, message_id, timestamp),
        )

    def settle(self, token: str) -> bool:
        self._require_transaction()
        a = self.db.execute(
            """SELECT a.*,r.telegram_message_id,r.recorded_at FROM telegram_send_attempts a
               JOIN telegram_send_receipts r USING(attempt_id) WHERE attempt_id=?""",
            (token,),
        ).fetchone()
        if a is None:
            return False
        kind, target, part = a["target_kind"], a["target_id"], a["part_index"]
        if self._binding(kind, target, part) != a["binding_hash"]:
            return False
        # Historical consent and newer attempts retain their own snapshots/ownership.
        if self.db.execute(
            """SELECT 1 FROM telegram_send_attempts WHERE target_kind=? AND target_id=?
               AND part_index=? AND sequence>?""",
            (kind, target, part, a["sequence"]),
        ).fetchone():
            return False
        if self.db.execute(
            """SELECT 1 FROM telegram_delivery_control_dispositions
               WHERE (target_kind='final_outbox' AND outbox_id=? AND ?='final')
                  OR (target_kind='progress_delivery' AND progress_id=? AND ?='progress')
               UNION ALL SELECT 1 FROM telegram_delivery_hold_dispositions
               WHERE outbox_id=? AND ?='final'
               UNION ALL SELECT 1 FROM telegram_delivery_hold_remote_dispositions
               WHERE outbox_id=? AND ?='final'""",
            (target, kind, target, kind, target, kind, target, kind),
        ).fetchone():
            return False
        table, key = (
            ("telegram_outbox", "outbox_id")
            if kind == "final"
            else ("provider_progress_deliveries", "progress_id")
        )
        row = self.db.execute(f"SELECT * FROM {table} WHERE {key}=?", (target,)).fetchone()
        if (
            row is None
            or row["status"] not in {"sending", "unknown"}
            or row["send_started_at"] != a["started_at"]
            or (row["status"] == "sending" and row["lease_token"] != token)
        ):
            return False
        if kind == "final":
            job = self.db.execute(
                "SELECT status FROM provider_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            allowed = {"result_ready", "failed", "indeterminate"}
            if row["sender_agent_id"] == "hub":
                allowed.add("cancelled")
            if job is None or job[0] not in allowed:
                return False
        message, timestamp = a["telegram_message_id"], a["recorded_at"]
        remaining = False
        if kind == "final":
            expected = self.db.execute(
                """SELECT MIN(part_index) FROM telegram_outbox_parts
                   WHERE outbox_id=? AND telegram_message_id IS NULL""",
                (target,),
            ).fetchone()[0]
            if expected != part:
                return False
            self.db.execute(
                """UPDATE telegram_outbox_parts SET telegram_message_id=?,delivered_at=?,
                   receipt_validation_version=1 WHERE outbox_id=? AND part_index=?""",
                (message, timestamp, target, part),
            )
            remaining = (
                self.db.execute(
                    """SELECT 1 FROM telegram_outbox_parts WHERE outbox_id=?
                   AND telegram_message_id IS NULL LIMIT 1""",
                    (target,),
                ).fetchone()
                is not None
            )
        if remaining:
            self.db.execute(
                """UPDATE telegram_outbox SET status='pending',attempt_count=MAX(attempt_count-1,0),
                   available_at=?,updated_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,send_started_at=NULL,error_code=NULL WHERE outbox_id=?""",
                (timestamp, timestamp, target),
            )
        else:
            self.db.execute(
                f"""UPDATE {table} SET status='delivered',telegram_message_id=?,delivered_at=?,
                    updated_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                    error_code=NULL WHERE {key}=?""",
                (message, timestamp, timestamp, target),
            )
            if kind == "final":
                self.retire_terminal_artifacts(target)
                self.db.execute(
                    """UPDATE provider_jobs SET status='completed',updated_at=?
                       WHERE job_id=? AND status='result_ready'""",
                    (timestamp, row["job_id"]),
                )
        return True

    def pending(self, kind: str, senders: tuple[str, ...]) -> tuple[str, ...]:
        if not senders:
            return ()
        table, key = (
            ("telegram_outbox", "outbox_id")
            if kind == "final"
            else ("provider_progress_deliveries", "progress_id")
        )
        placeholders = ",".join("?" for _ in senders)
        query = f"""SELECT a.attempt_id,a.sequence FROM telegram_send_attempts a
                JOIN telegram_send_receipts r USING(attempt_id)
                JOIN {table} t ON t.{key}=a.target_id
                WHERE a.target_kind=? AND a.sequence>? AND t.status IN ('sending','unknown')
                  AND t.sender_agent_id IN ({placeholders})
                  AND NOT EXISTS (SELECT 1 FROM telegram_delivery_control_dispositions d
                       WHERE d.outbox_id=t.{key} OR d.progress_id=t.{key})
                  AND NOT EXISTS (SELECT 1 FROM telegram_delivery_hold_dispositions h
                       WHERE h.outbox_id=t.{key})
                  AND NOT EXISTS (SELECT 1 FROM telegram_delivery_hold_remote_dispositions h
                       WHERE h.outbox_id=t.{key})
                ORDER BY a.sequence LIMIT 64"""
        cursor_key = (kind, senders)
        cursor = self._cursors.get(cursor_key, 0)
        rows = self.db.execute(query, (kind, cursor, *senders)).fetchall()
        if not rows and cursor:
            rows = self.db.execute(query, (kind, 0, *senders)).fetchall()
        # Archived/nonprojectable evidence must not monopolize every maintenance
        # page. A restart resets this local cursor but never deletes proof.
        self._cursors[cursor_key] = int(rows[-1][1]) if rows else 0
        return tuple(str(r[0]) for r in rows)

    def snapshot(self, kind: str, target: str) -> list[dict[str, object]]:
        """Complete evidence for explicit owner snapshot CAS, without new authority."""
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT a.*,r.telegram_message_id,r.recorded_at FROM telegram_send_attempts a
               LEFT JOIN telegram_send_receipts r USING(attempt_id)
               WHERE target_kind=? AND target_id=? ORDER BY sequence""",
                (kind, target),
            )
        ]
