"""Exact ingress authority and historical notices; no native I/O or scheduling."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from .codex_ingress_assessments import CodexIngressAssessments
from .codex_turn_controls import (
    ActiveTurnProof,
    CodexTurnControls,
    _now,
    _validate_interrupt_selection,
)
from .state_errors import StateError
from .stop_coverage import STOP_COVERS_JOB_SQL
from .task_lifecycle import TaskLifecycleState

INGRESS_PRECAUTION_NOTICE = (
    "Hub reserved a precautionary interrupt when reliable Telegram ingress became unavailable or unconfirmed for this exact turn. "
    "This earlier reservation was a Hub precaution, separate from any owner /stop. It does not prove transmission or "
    "termination. Saved turn outcome and confirmed owner delivery remain separate. "
    "The task was not replayed; inspect /status before further work."
)


class CodexIngressControl:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
        controls: CodexTurnControls,
        assessments: CodexIngressAssessments,
        notices: TaskLifecycleState,
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.controls = controls
        self.assessments = assessments
        self.notices = notices

    def read_cause(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM codex_ingress_interrupt_causes WHERE job_id=?", (job_id,)
        ).fetchone()

    def prepare_notice(self, job_id: str) -> bool:
        """Historical frozen-cause notice, independently fenced by the Hub sender.

        Runtime calls this after the native helper settles, never between the
        interrupt reservation and RPC. Failure cannot revoke a control fence.
        Delivery neither resolves ingress/native uncertainty nor claims stop.
        """
        if self.read_cause(job_id) is None:
            return False
        with self.transaction():
            if self.read_cause(job_id) is None:
                return False
            row = self.db.execute(
                "SELECT job.chat_id,job.message_id,topic.thread_id FROM provider_jobs job "
                "JOIN topics topic ON topic.topic_id=job.topic_id WHERE job.job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise StateError("ingress notice lost its exact job")
            _, created = self.notices.prepare_notice_in_transaction(
                event_key="codex_ingress_control:" + job_id,
                kind="codex_ingress_control",
                job_id=job_id,
                chat_id=row["chat_id"],
                thread_id=row["thread_id"],
                reply_to_message_id=row["message_id"],
                telegram_html=INGRESS_PRECAUTION_NOTICE,
                now=_now(None),
            )
        return created

    def candidate_page(
        self,
        agent_id: str,
        *,
        after: str | None = None,
        through: str | None = None,
        limit: int = 32,
        now: datetime | None = None,
    ) -> tuple[tuple[str, ...], str | None]:
        """Bounded advisory keyset scan; only claim_read can allocate authority.

        The captured upper key bounds one sweep against continuous insertions.
        This page query takes no write lock; claim_read reassesses each candidate
        transactionally, including healthy candidates. Post-send targets
        remain eligible for terminal/result observation, never another send.
        """
        if not agent_id or len(agent_id) > 64 or type(limit) is not int or not 1 <= limit <= 32:
            raise StateError("invalid ingress candidate page")
        timestamp = _now(now).isoformat()
        selection = """FROM codex_turn_controls control
            JOIN codex_telegram_precaution_targets target ON target.job_id=control.job_id
            JOIN provider_jobs job ON job.job_id=control.job_id
            JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id=control.job_id
            WHERE control.agent_id=? AND control.origin='accepted_v48'
              AND job.status='indeterminate' AND job.lease_token IS NULL
              AND checkpoint.completed_text IS NULL AND control.late_read_attempts<3
              AND (control.next_late_read_at IS NULL OR control.next_late_read_at<=?)
              AND (control.read_claim_token IS NULL OR control.read_claim_expires_at<=?)
              AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal
                              WHERE terminal.job_id=control.job_id)
              AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolved
                              WHERE resolved.job_id=control.job_id)"""
        parameters = (agent_id, timestamp, timestamp)
        if through is None:
            through = self.db.execute(
                "SELECT MAX(control.job_id) " + selection, parameters
            ).fetchone()[0]
        if through is None:
            return (), None
        rows = self.db.execute(
            "SELECT control.job_id "
            + selection
            + " AND control.job_id>? AND control.job_id<=? ORDER BY control.job_id LIMIT ?",
            (*parameters, after or "", through, limit),
        ).fetchall()
        return tuple(str(row[0]) for row in rows), through

    def _fresh_target(self, job_id: str) -> bool:
        return (
            self.db.execute(
                """SELECT 1 FROM codex_telegram_precaution_targets target
               JOIN codex_turn_controls control ON control.job_id=target.job_id
               WHERE target.job_id=? AND control.origin='accepted_v48'""",
                (job_id,),
            ).fetchone()
            is not None
        )

    def _due_revision(self, job_id: str, *, now: datetime) -> int | None:
        assessment = self.assessments.assess_in_transaction(job_id, now=now)
        if assessment.episode is None or assessment.episode.deadline > now:
            return None
        row = self.assessments.read(job_id)
        assert row is not None
        return int(row["assessment_revision"])

    def _pending_stop(self, job_id: str) -> bool:
        return (
            self.db.execute(
                f"""SELECT 1 FROM provider_stop_requests stop
                JOIN provider_jobs job ON job.job_id=?
                WHERE stop.status='pending' AND {STOP_COVERS_JOB_SQL}""",
                (job_id,),
            ).fetchone()
            is not None
        )

    def begin_interrupt(
        self,
        *,
        job_id: str,
        proof: ActiveTurnProof,
        validated_root: str,
        invocation_token: str | None = None,
        read_claim_token: str | None = None,
        send_deadline: float | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """Commit reassessment, permanent fence and frozen cause together.

        Invocation authority uses the protective path; a read claim uses late.
        Neither path fabricates owner-stop provenance. The returned token is
        usable only after commit, with a fresh proof/deadline check before RPC.
        Storage/assessment faults propagate to the optional ingress caller,
        which must not treat them as loss of the mandatory native stream.
        Runtime callers must leave now=None for refreshed wall-clock validation;
        an explicit now is a fixed fixture clock, not a production time source.
        """
        source = "late" if read_claim_token is not None else "protective"
        _validate_interrupt_selection(source, invocation_token, read_claim_token)
        with self.transaction():
            current = _now(now)
            self.controls.bind_covering_stops_in_transaction()
            if self._pending_stop(job_id):
                return None
            if not self._fresh_target(job_id):
                return None
            row = self.controls._validated_target_in_transaction(
                job_id=job_id,
                proof=proof,
                validated_root=validated_root,
                invocation_token=invocation_token,
                read_claim_token=read_claim_token,
                send_deadline=send_deadline,
                now=current,
            )
            if row is None:
                return None
            revision = self._due_revision(job_id, now=current)
            if revision is None:
                return None
            # Assessment/SQLite work may consume the observation or RPC budget.
            if (
                self.controls._validated_target_in_transaction(
                    job_id=job_id,
                    proof=proof,
                    validated_root=validated_root,
                    invocation_token=invocation_token,
                    read_claim_token=read_claim_token,
                    send_deadline=send_deadline,
                    now=_now(now),
                )
                is None
            ):
                return None
            owner = self.controls._reserve_in_transaction(
                job_id,
                source,
                now=current,
                ingress_assessment_revision=revision,
            )
        return owner

    def claim_read(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> sqlite3.Row | None:
        """Consume the shared three-cycle allowance before connecting.

        A covering real pending stop defers to the existing stop-only consumer.
        Recovery, another episode or another stop never replenish the allowance.
        While ingress is still due, an ingress or native send-start fence permits
        bounded exact terminal/result observation only. It never permits resend,
        provenance enrichment or sender quiescence. Recovery, terminal proof or
        owner resolution suppress further ingress claims; this is not an
        unconditional maintenance allocator.
        Worker-owned maintenance is the caller; this domain owns no scheduler.
        """
        if not worker_id or len(worker_id) > 128:
            raise StateError("invalid control read owner")
        with self.transaction():
            current = _now(now)
            self.controls.bind_covering_stops_in_transaction()
            if not self._fresh_target(job_id):
                return None
            control = self.controls.read(job_id)
            job = self.db.execute(
                "SELECT * FROM provider_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if (
                control is None
                or job is None
                or job["status"] != "indeterminate"
                or job["lease_token"] is not None
                or not self.controls._coherent_target(control, job)
                or self._pending_stop(job_id)
            ):
                return None
            if self._due_revision(job_id, now=current) is None:
                return None
            claim = self.controls._allocate_read_in_transaction(job_id, worker_id, now=current)
        return claim
