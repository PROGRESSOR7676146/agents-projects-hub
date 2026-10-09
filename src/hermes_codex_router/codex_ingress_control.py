"""Dormant exact ingress control authority; no native I/O or scheduling."""

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


class CodexIngressControl:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
        controls: CodexTurnControls,
        assessments: CodexIngressAssessments,
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.controls = controls
        self.assessments = assessments

    def read_cause(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM codex_ingress_interrupt_causes WHERE job_id=?", (job_id,)
        ).fetchone()

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
        This method is deliberately not wired into worker maintenance yet.
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
