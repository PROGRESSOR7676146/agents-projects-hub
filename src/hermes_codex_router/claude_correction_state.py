"""Input SQL under the existing HubState transaction; no I/O or invocation."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .claude_corrections import MAX_CORRECTION_CHARACTERS, ClaudeInputFrame
from .codex_control_predicates import control_owner_for_topic
from .delivery_hold_predicates import job_blocks_topic_fifo
from .state_errors import StateError
from .stop_coverage import pending_stop_for_job
from .telegram_turn_provenance import TelegramTurnProvenance

if TYPE_CHECKING:
    from .queue_visibility import QueueVisibilityState


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ClaudeCorrectionState:
    def __init__(
        self, connection: sqlite3.Connection, *, visibility: QueueVisibilityState | None = None
    ):
        self.db = connection
        self.visibility = visibility

    def _sync(self, child: str, timestamp: str, *, executing: bool = False) -> None:
        if self.visibility is not None:
            sync = (
                self.visibility.executing_in_transaction
                if executing
                else self.visibility.sync_job_in_transaction
            )
            sync(child, now=datetime.fromisoformat(timestamp))

    def available(self) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claude_input_children'"
            ).fetchone()
            is not None
        )

    def _transaction(self) -> None:
        if not self.db.in_transaction:
            raise StateError("Claude input requires its owning state transaction")

    def child(self, parent: str) -> sqlite3.Row | None:
        if not self.available():
            return None
        return self.db.execute(
            "SELECT * FROM claude_input_children WHERE parent_job_id=?", (parent,)
        ).fetchone()

    def _owner(self, parent: str, token: str) -> sqlite3.Row:
        self._transaction()
        row = self.db.execute(
            """SELECT j.*, p.provider_thread_id AS native, p.project_root AS root,
               p.completed_text, s.provider_session_id AS current_native,
               s.generation AS current_generation, s.writer_mode, s.status AS session_status,
               s.model AS current_model, s.effort AS current_effort,
               t.execution_scope, t.project_id, t.chat_id AS current_chat
               FROM provider_jobs j JOIN provider_execution_checkpoints p ON p.job_id=j.job_id
               JOIN agent_sessions s ON s.session_id=j.session_id JOIN topics t ON t.topic_id=j.topic_id
               WHERE j.job_id=? AND j.status='executing' AND j.lease_token=? AND j.lease_expires_at>?""",
            (parent, token, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if row is None or (
            row["native"] != row["current_native"]
            or row["session_generation"] != row["current_generation"]
            or row["writer_mode"] != "telegram"
            or row["session_status"] == "archived"
            or row["current_chat"] != row["chat_id"]
            or row["execution_scope"] not in (f"root:{row['root']}", f"project:{row['project_id']}")
            or row["completed_text"] is not None
        ):
            raise StateError("Claude input owner binding changed")
        return row

    def prepare(self, parent: str, token: str, frame: ClaudeInputFrame, root: str) -> None:
        row = self._owner(parent, token)
        if frame.session_id != row["native"] or root != row["root"]:
            raise StateError("Claude input preparation binding changed")
        frame.encode()
        self.db.execute(
            "INSERT INTO claude_input_runs(parent_job_id,initial_input_uuid,initial_digest,native_session_id,project_root) VALUES(?,?,?,?,?)",
            (parent, frame.uuid, _digest(frame.text), frame.session_id, root),
        )

    def claim(self, parent: str, token: str) -> ClaudeInputFrame | None:
        try:
            row = self._owner(parent, token)
        except StateError:
            return None
        run = self.db.execute(
            "SELECT * FROM claude_input_runs WHERE parent_job_id=?", (parent,)
        ).fetchone()
        if (
            run is None
            or row["model"] != row["current_model"]
            or row["effort"] != row["current_effort"]
            or run["terminal_count"] != 0
            or self.child(parent) is not None
            or pending_stop_for_job(self.db, parent) is not None
            or control_owner_for_topic(self.db, int(row["topic_id"])) is not None
        ):
            return None
        candidate = self.db.execute(
            f"""SELECT * FROM provider_jobs j WHERE j.topic_id=? AND j.topic_sequence>?
               AND {job_blocks_topic_fifo("j")} ORDER BY j.topic_sequence LIMIT 1""",
            (row["topic_id"], row["topic_sequence"]),
        ).fetchone()
        if candidate is None or (
            candidate["status"] != "queued"
            or not 0 < len(candidate["payload_text"]) <= MAX_CORRECTION_CHARACTERS
            or candidate["attempt_count"] >= candidate["max_attempts"]
            or any(
                candidate[field] != row[field]
                for field in (
                    "chat_id",
                    "topic_id",
                    "agent_id",
                    "session_id",
                    "session_generation",
                    "model",
                    "effort",
                    "codex_permission_profile",
                )
            )
            or candidate["provider_session_id"] not in (None, row["native"])
            or candidate["context_watermark"] is not None
            or candidate["handoff_id"] is not None
            or candidate["next_attempt_at"] is not None
            and candidate["next_attempt_at"] > datetime.now(timezone.utc).isoformat()
            or TelegramTurnProvenance(self.db).identity(str(candidate["job_id"]))
            != TelegramTurnProvenance(self.db).identity(parent)
            or pending_stop_for_job(self.db, str(candidate["job_id"])) is not None
            or self.db.execute(
                "SELECT 1 FROM provider_job_holds WHERE job_id=? AND decision='pending'",
                (candidate["job_id"],),
            ).fetchone()
            or self.db.execute(
                "SELECT 1 FROM incoming_materials WHERE job_id=?", (candidate["job_id"],)
            ).fetchone()
            or self.db.execute(
                "SELECT 1 FROM provider_preexecution_retries WHERE child_job_id=?",
                (candidate["job_id"],),
            ).fetchone()
        ):
            return None
        child_token = str(uuid.uuid4())
        frame = ClaudeInputFrame(
            str(uuid.uuid4()), str(row["native"]), str(candidate["payload_text"])
        )
        frame.encode()
        if frame.uuid == run["initial_input_uuid"]:
            return None
        timestamp = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO claude_input_children(parent_job_id,child_job_id,child_lease_token,input_uuid,input_digest) VALUES(?,?,?,?,?)",
            (parent, candidate["job_id"], child_token, frame.uuid, _digest(frame.text)),
        )
        changed = self.db.execute(
            """UPDATE provider_jobs SET status='executing',attempt_count=attempt_count+1,
               provider_started_at=?,updated_at=?,lease_owner=?,lease_token=?,lease_expires_at=?,next_attempt_at=NULL
               WHERE job_id=? AND status='queued'""",
            (
                timestamp,
                timestamp,
                row["lease_owner"],
                child_token,
                row["lease_expires_at"],
                candidate["job_id"],
            ),
        )
        if changed.rowcount != 1:
            raise StateError("Claude correction reservation changed")
        self._sync(str(candidate["job_id"]), timestamp, executing=True)
        return frame

    def receipt(self, parent: str, token: str, identifier: str, boundary: int) -> None:
        self._owner(parent, token)
        run = self.db.execute(
            "SELECT * FROM claude_input_runs WHERE parent_job_id=?", (parent,)
        ).fetchone()
        child = self.child(parent)
        if run is None or type(boundary) is not int or boundary != run["terminal_count"] + 1:
            raise StateError("Claude input receipt boundary changed")
        if (
            identifier == run["initial_input_uuid"]
            and boundary == 1
            and run["initial_receipt_terminal"] is None
        ):
            self.db.execute(
                "UPDATE claude_input_runs SET initial_receipt_terminal=1 WHERE parent_job_id=?",
                (parent,),
            )
        elif (
            child is not None
            and identifier == child["input_uuid"]
            and child["receipt_terminal"] is None
            and run["initial_receipt_terminal"] == 1
        ):
            self.db.execute(
                "UPDATE claude_input_children SET receipt_terminal=? WHERE parent_job_id=?",
                (boundary, parent),
            )
        else:
            raise StateError("Claude input receipt binding changed")

    def result(self, parent: str, token: str, boundary: int) -> None:
        self._owner(parent, token)
        changed = self.db.execute(
            "UPDATE claude_input_runs SET terminal_count=? WHERE parent_job_id=? AND terminal_count=? AND initial_receipt_terminal=1",
            (boundary, parent, boundary - 1),
        )
        if type(boundary) is not int or boundary not in (1, 2) or changed.rowcount != 1:
            raise StateError("Claude input terminal sequence changed")

    def validate_completion(self, parent: str) -> None:
        self._transaction()
        if not self.available():
            return
        run = self.db.execute(
            "SELECT * FROM claude_input_runs WHERE parent_job_id=?", (parent,)
        ).fetchone()
        if run is None:
            return
        child = self.child(parent)
        if (
            run["initial_receipt_terminal"] != 1
            or not run["terminal_count"]
            or child is not None
            and (
                child["receipt_terminal"] is None
                or child["receipt_terminal"] > run["terminal_count"]
            )
        ):
            raise StateError("Claude input completion is unconfirmed")
        self.db.execute(
            "UPDATE claude_input_runs SET completion_validated=1 WHERE parent_job_id=?", (parent,)
        )

    def heartbeat(self, parent: str, timestamp: str, expires: str) -> None:
        self._transaction()
        if not self.available():
            return
        self.db.execute(
            """UPDATE provider_jobs SET lease_expires_at=?,updated_at=? WHERE status='executing'
               AND (job_id,lease_token) IN (SELECT child_job_id,child_lease_token FROM claude_input_children WHERE parent_job_id=?)""",
            (expires, timestamp, parent),
        )

    def settle(self, parent: str, disposition: str, timestamp: str) -> None:
        self._transaction()
        child = self.child(parent)
        if child is None:
            return
        if disposition == "completed":
            run = self.db.execute(
                "SELECT completion_validated FROM claude_input_runs WHERE parent_job_id=?",
                (parent,),
            ).fetchone()
            saved = self.db.execute(
                "SELECT completed_text FROM provider_execution_checkpoints WHERE job_id=?",
                (parent,),
            ).fetchone()
            if run is None or run[0] != 1 or saved is None or saved[0] is None:
                raise StateError("Claude correction has no saved completion")
        elif disposition != "cancelled":
            disposition = "indeterminate"
        changed = self.db.execute(
            """UPDATE provider_jobs SET status=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
               error_class=?,error_code=?,updated_at=? WHERE job_id=? AND status='executing' AND lease_token=?""",
            (
                disposition,
                "ambiguous_execution"
                if disposition == "indeterminate"
                else "user_stop"
                if disposition == "cancelled"
                else None,
                "claude_correction_unconfirmed"
                if disposition == "indeterminate"
                else "emergency_stop"
                if disposition == "cancelled"
                else None,
                timestamp,
                child["child_job_id"],
                child["child_lease_token"],
            ),
        )
        if changed.rowcount != 1:
            raise StateError("Claude correction lease changed before settlement")
        self._sync(str(child["child_job_id"]), timestamp)

    def complete(self, parent: str, timestamp: str) -> list[str]:
        self.settle(parent, "completed", timestamp)
        if not self.available():
            return []
        return [
            str(row[0])
            for row in self.db.execute(
                "SELECT j.payload_text FROM claude_input_children c JOIN provider_jobs j ON j.job_id=c.child_job_id WHERE c.parent_job_id=?",
                (parent,),
            ).fetchall()
        ]
