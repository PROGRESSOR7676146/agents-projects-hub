"""State-owned exact-target control journal; never invokes a provider."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .codex_control_predicates import resolved_control_scope
from .state_errors import StateError
from .stop_coverage import STOP_COVERS_JOB_SQL


@dataclass(frozen=True, slots=True)
class ActiveTurnProof:
    """Fresh exact-active observation from an owning no-fallback native client."""

    thread_id: str
    turn_id: str
    root: str
    observed_monotonic: float


InterruptSource = Literal["live", "protective", "late", "permission_drift"]
InterruptOutcome = Literal["matched_ack", "matched_rejection", "not_sent", "unknown"]


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise StateError("control time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _validate_interrupt_selection(
    source: InterruptSource, invocation_token: str | None, read_claim_token: str | None
) -> None:
    if (invocation_token is None) == (read_claim_token is None):
        raise StateError("interrupt requires exactly one invocation or read-claim authority")
    if source not in {"live", "protective", "late", "permission_drift"}:
        raise StateError("invalid interrupt source")
    if (source == "late") != (read_claim_token is not None):
        raise StateError("read-claim control must use the late source")


class CodexTurnControls:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
    ) -> None:
        self.db = connection
        self.transaction = transaction

    def read(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM codex_turn_controls WHERE job_id=?", (job_id,)
        ).fetchone()

    def claim_late_read(
        self, worker_id: str, *, agent_id: str = "codex", now: datetime | None = None
    ) -> sqlite3.Row | None:
        """Consume one of three cycles before any connection or native read.

        Claim expiry permits another observation only. It cannot reset a send
        fence or establish that its owning process or RPC has stopped.
        """
        if not worker_id or len(worker_id) > 128:
            raise StateError("invalid control read owner")
        if not agent_id or len(agent_id) > 64:
            raise StateError("invalid control runtime agent")
        selection = """SELECT control.* FROM codex_turn_controls control
                   JOIN provider_jobs job ON job.job_id=control.job_id
                   JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id=job.job_id
                   JOIN provider_stop_requests stop ON stop.request_id=control.stop_request_id
                   WHERE job.status='indeterminate' AND job.lease_token IS NULL AND control.agent_id=?
                     AND checkpoint.completed_text IS NULL
                     AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal WHERE terminal.job_id=job.job_id)
                     AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolved WHERE resolved.job_id=job.job_id)
                     AND stop.status='pending' AND control.late_read_attempts<3
                     AND control.next_late_read_at<=?
                     AND (control.read_claim_token IS NULL OR control.read_claim_expires_at<=?)
                   ORDER BY control.next_late_read_at,control.job_id LIMIT 1"""
        timestamp = _now(now).isoformat()
        # Negative snapshots only defer work until the next poll. Authority and
        # claim consumption still require the unchanged locked recheck below.
        if self.db.execute(selection, (agent_id, timestamp, timestamp)).fetchone() is None:
            unbound = self.db.execute(
                f"""SELECT 1 FROM provider_stop_requests stop
                    WHERE stop.status='pending' AND EXISTS (
                        SELECT 1 FROM codex_turn_controls control
                        JOIN provider_jobs job ON job.job_id=control.job_id
                        WHERE control.agent_id=? AND control.stop_request_id IS NULL
                          AND {STOP_COVERS_JOB_SQL}) LIMIT 1""",
                (agent_id,),
            ).fetchone()
            if unbound is None:
                return None
        with self.transaction():
            current = _now(now)
            timestamp = current.isoformat()
            self.bind_covering_stops_in_transaction()
            row = self.db.execute(
                selection,
                (agent_id, timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.db.execute(
                """UPDATE codex_turn_controls SET late_read_attempts=late_read_attempts+1,
                   next_late_read_at=?,read_claim_token=?,read_claim_owner=?,read_claim_expires_at=?
                   WHERE job_id=?""",
                (
                    (current + timedelta(seconds=30)).isoformat(),
                    token,
                    worker_id,
                    (current + timedelta(seconds=30)).isoformat(),
                    row["job_id"],
                ),
            )
            return self.read(str(row["job_id"]))

    def finish_late_read(self, job_id: str, claim_token: str) -> None:
        """Release only this observation claim, never send-owner authority."""
        with self.transaction():
            self.db.execute(
                """UPDATE codex_turn_controls SET read_claim_token=NULL,
                   read_claim_owner=NULL,read_claim_expires_at=NULL
                   WHERE job_id=? AND read_claim_token=?""",
                (job_id, claim_token),
            )

    def begin_interrupt(
        self,
        *,
        job_id: str,
        source: InterruptSource,
        proof: ActiveTurnProof,
        validated_root: str,
        invocation_token: str | None = None,
        read_claim_token: str | None = None,
        send_deadline: float | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically fence one exact interrupt, with mutually exclusive authority."""
        _validate_interrupt_selection(source, invocation_token, read_claim_token)
        with self.transaction():
            return self.begin_interrupt_in_transaction(
                job_id=job_id,
                source=source,
                proof=proof,
                validated_root=validated_root,
                invocation_token=invocation_token,
                read_claim_token=read_claim_token,
                send_deadline=send_deadline,
                now=now,
            )

    def begin_interrupt_in_transaction(
        self,
        *,
        job_id: str,
        source: InterruptSource,
        proof: ActiveTurnProof,
        validated_root: str,
        invocation_token: str | None = None,
        read_claim_token: str | None = None,
        send_deadline: float | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """Reserve in the caller's transaction; use the token only after commit.

        Existing source and authority rules still apply. Assessment evidence
        does not independently authorize an interrupt or a native call.
        """
        if not self.db.in_transaction:
            raise StateError("interrupt reservation requires an owning transaction")
        _validate_interrupt_selection(source, invocation_token, read_claim_token)
        current = _now(now).isoformat()
        row = self.read(job_id)
        if row is None or row["origin"] != "accepted_v48" or row["send_started_at"] is not None:
            return None
        if (
            not isinstance(proof, ActiveTurnProof)
            or type(proof.observed_monotonic) not in (float, int)
            or not math.isfinite(proof.observed_monotonic)
            or not 0 <= time.monotonic() - proof.observed_monotonic <= 5
            or proof.thread_id != row["provider_thread_id"]
            or proof.turn_id != row["provider_turn_id"]
            or proof.root != row["project_root"]
            or validated_root != row["project_root"]
            or (send_deadline is not None and time.monotonic() >= send_deadline)
        ):
            return None
        job = self.db.execute("SELECT * FROM provider_jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            return None
        if invocation_token is not None:
            if (
                job["status"] != "executing"
                or job["lease_token"] != invocation_token
                or job["lease_expires_at"] is None
                or job["lease_expires_at"] <= current
                or row["read_claim_token"] is not None
            ):
                return None
        elif (
            job["status"] != "indeterminate"
            or job["lease_token"] is not None
            or row["read_claim_token"] != read_claim_token
            or row["read_claim_expires_at"] is None
            or row["read_claim_expires_at"] <= current
        ):
            return None
        if not self._coherent_target(row, job):
            return None
        if (
            source in {"live", "late"}
            and self.db.execute(
                f"""SELECT 1 FROM provider_stop_requests stop
                JOIN provider_jobs job ON job.job_id=?
                WHERE stop.request_id=? AND stop.status='pending' AND {STOP_COVERS_JOB_SQL}""",
                (job_id, row["stop_request_id"]),
            ).fetchone()
            is None
        ):
            return None
        owner = uuid.uuid4().hex
        changed = self.db.execute(
            """UPDATE codex_turn_controls SET send_owner_token_hash=?,send_started_at=?,
               interrupt_source=? WHERE job_id=? AND send_started_at IS NULL""",
            (hashlib.sha256(owner.encode()).hexdigest(), current, source, job_id),
        )
        return owner if changed.rowcount == 1 else None

    def _coherent_target(self, row: sqlite3.Row, job: sqlite3.Row) -> bool:
        target = self.db.execute(
            """SELECT session.*,topic.project_id,topic.chat_id,topic.thread_id,
                      checkpoint.provider_thread_id AS saved_thread,
                      checkpoint.provider_turn_id AS saved_turn,checkpoint.project_root AS saved_root,
                      checkpoint.codex_permission_profile AS saved_profile,checkpoint.completed_text
               FROM agent_sessions session JOIN topics topic ON topic.topic_id=session.topic_id
               JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id=?
               WHERE session.session_id=?""",
            (row["job_id"], row["session_id"]),
        ).fetchone()
        if target is None or any(
            job[key] != row[key]
            for key in ("session_id", "session_generation", "topic_id", "agent_id")
        ):
            return False
        if not (
            job["chat_id"] == row["input_chat_id"]
            and target["topic_id"] == row["topic_id"]
            and target["agent_id"] == row["agent_id"]
            and target["generation"] == row["session_generation"]
            and target["status"] in {"active", "satellite"}
            and target["writer_mode"] == "telegram"
            and target["provider_session_id"] == target["saved_thread"] == row["provider_thread_id"]
            and target["saved_turn"] == row["provider_turn_id"]
            and target["saved_root"] == row["project_root"]
            and target["project_id"] == row["project_id"]
            and target["chat_id"] == row["chat_id"]
            and target["thread_id"] == row["thread_id"]
            and target["completed_text"] is None
            and target["codex_permission_profile"]
            == target["saved_profile"]
            == job["codex_permission_profile"]
            == row["codex_permission_profile"]
        ):
            return False
        if (
            self.db.execute(
                """SELECT 1 FROM provider_turn_terminal_evidence WHERE job_id=?
               UNION SELECT 1 FROM provider_job_resolutions WHERE job_id=?""",
                (row["job_id"], row["job_id"]),
            ).fetchone()
            is not None
        ):
            return False
        return (
            resolved_control_scope(self.db, int(row["topic_id"])) == f"root:{row['project_root']}"
        )

    def finish_interrupt(
        self,
        job_id: str,
        owner_token: str,
        *,
        outcome: InterruptOutcome,
        send_path_quiesced: bool,
        now: datetime | None = None,
    ) -> None:
        """Only a matched response and ended send path can quiesce that owner.

        Native terminality, TTL, close, timeout and a finally block are not proof.
        Even a quiesced sender retains its permanent single-send fence.
        """
        if outcome not in {"matched_ack", "matched_rejection", "not_sent", "unknown"}:
            raise StateError("invalid interrupt outcome")
        if send_path_quiesced and outcome == "unknown":
            raise StateError("unknown interrupt cannot prove sender quiescence")
        with self.transaction():
            row = self.read(job_id)
            if (
                row is None
                or row["send_owner_token_hash"] != hashlib.sha256(owner_token.encode()).hexdigest()
                or row["send_started_at"] is None
            ):
                raise StateError("interrupt owner does not match retained send-start")
            if row["interrupt_outcome"] not in (None, "unknown", outcome):
                raise StateError("matched interrupt outcome is immutable")
            self.db.execute(
                """UPDATE codex_turn_controls SET interrupt_outcome=?,
                   owner_quiesced_at=COALESCE(owner_quiesced_at,?) WHERE job_id=?""",
                (outcome, _now(now).isoformat() if send_path_quiesced else None, job_id),
            )

    def accept_in_transaction(
        self, job: sqlite3.Row, checkpoint: dict[str, str | None], turn_id: str
    ) -> bool:
        """Freeze coherent authority without discarding native acceptance evidence.

        A domain refusal is a result, not a transaction failure. Storage faults
        still propagate. Repeating retained acceptance never invents authority.
        """
        if not self.db.in_transaction:
            raise StateError("control acceptance requires its checkpoint transaction")
        prior = self.read(str(job["job_id"]))
        if checkpoint["provider_turn_id"] is not None:
            # Old missing rows and migrated rows never gain fresh send authority.
            if prior is not None and (
                prior["provider_turn_id"] != turn_id
                or prior["provider_thread_id"] != checkpoint["provider_thread_id"]
                or prior["project_root"] != checkpoint["project_root"]
            ):
                raise StateError("accepted control target differs from its checkpoint")
            return True
        if prior is not None:
            return False
        session = self.db.execute(
            "SELECT * FROM agent_sessions WHERE session_id=?", (job["session_id"],)
        ).fetchone()
        topic = self.db.execute(
            "SELECT * FROM topics WHERE topic_id=?", (job["topic_id"],)
        ).fetchone()
        root = checkpoint["project_root"]
        if (
            session is None
            or topic is None
            or root is None
            or session["topic_id"] != job["topic_id"]
            or session["agent_id"] != job["agent_id"]
            or session["generation"] != job["session_generation"]
            or session["status"] not in {"active", "satellite"}
            or session["writer_mode"] != "telegram"
            or session["provider_session_id"] != checkpoint["provider_thread_id"]
            or not (
                session["codex_permission_profile"]
                == job["codex_permission_profile"]
                == checkpoint["codex_permission_profile"]
            )
            or topic["execution_scope"]
            not in (f"root:{root}", f"project:{topic['project_id']}", None)
        ):
            return False
        if (
            self.db.execute(
                """SELECT 1 FROM codex_turn_controls WHERE provider_thread_id=?
               AND provider_turn_id=?
               UNION SELECT 1 FROM provider_execution_checkpoints WHERE provider_thread_id=?
               AND provider_turn_id=? AND job_id!=? LIMIT 1""",
                (
                    checkpoint["provider_thread_id"],
                    turn_id,
                    checkpoint["provider_thread_id"],
                    turn_id,
                    job["job_id"],
                ),
            ).fetchone()
            is not None
        ):
            return False
        self.db.execute(
            """INSERT INTO codex_turn_controls
               (job_id,origin,session_id,session_generation,topic_id,agent_id,project_id,
                chat_id,input_chat_id,thread_id,execution_scope,provider_thread_id,provider_turn_id,
                project_root,codex_permission_profile,accepted_at)
               VALUES (?,'accepted_v48',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job["job_id"],
                job["session_id"],
                job["session_generation"],
                job["topic_id"],
                job["agent_id"],
                topic["project_id"],
                topic["chat_id"],
                job["chat_id"],
                topic["thread_id"],
                topic["execution_scope"] or f"project:{topic['project_id']}",
                checkpoint["provider_thread_id"],
                turn_id,
                root,
                checkpoint["codex_permission_profile"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        from .telegram_turn_provenance import TelegramTurnProvenance

        TelegramTurnProvenance(self.db).record_fresh_target_in_transaction(str(job["job_id"]))
        self.bind_covering_stops_in_transaction()
        return True

    def bind_covering_stops_in_transaction(self) -> None:
        """Also discovers retained pending stops after an upgrade, without new receipts."""
        if not self.db.in_transaction:
            raise StateError("control stop binding requires an owning transaction")
        rows = self.db.execute(
            f"""SELECT control.job_id,stop.request_id,stop.created_at
                FROM codex_turn_controls control JOIN provider_jobs job ON job.job_id=control.job_id
                JOIN provider_stop_requests stop ON {STOP_COVERS_JOB_SQL}
                WHERE control.stop_request_id IS NULL AND stop.status='pending'
                ORDER BY stop.created_at,stop.request_id"""
        ).fetchall()
        for row in rows:
            self.db.execute(
                """UPDATE codex_turn_controls SET stop_request_id=?,next_late_read_at=
                   CASE WHEN next_late_read_at IS NULL OR next_late_read_at < ?
                        THEN ? ELSE next_late_read_at END
                   WHERE job_id=? AND stop_request_id IS NULL""",
                (row["request_id"], row["created_at"], row["created_at"], row["job_id"]),
            )
