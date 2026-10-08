"""Transaction-bound admission provenance; presence never authorizes an interrupt."""

from __future__ import annotations

import sqlite3

from .codex_ingress_precaution_policy import IngressIdentity
from .state_errors import StateError


def validate_ingress_identity(identity: str | None) -> IngressIdentity | None:
    if identity is None:
        return None
    if identity == "hub":
        return "hub"
    if identity == "codex":
        return "codex"
    raise StateError("unsupported Telegram admission ingress")


def group_ingress_identity(
    identity: str | None, *, group_controller: bool
) -> IngressIdentity | None:
    """Explicit logical ingress only; no provider/observer/configuration fallback."""
    if group_controller and identity in {"hub", "codex"}:
        return validate_ingress_identity(identity)
    return None


class TelegramTurnProvenance:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection

    def identity(self, job_id: str) -> IngressIdentity | None:
        row = self.db.execute(
            "SELECT ingress_identity FROM provider_job_telegram_ingress WHERE job_id=?", (job_id,)
        ).fetchone()
        return None if row is None else validate_ingress_identity(row[0])

    def target(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM codex_telegram_precaution_targets WHERE job_id=?", (job_id,)
        ).fetchone()

    def record_new_job_in_transaction(self, job_id: str, ingress_identity: str | None) -> None:
        """Called only by the branch that just INSERTed a new job, before inputs.

        The defensive closure guard cannot establish freshness alone. No repair,
        historical enrichment or caller-supplied clock confers that provenance.
        """
        if not self.db.in_transaction:
            raise StateError("Telegram admission provenance requires its owner transaction")
        identity = validate_ingress_identity(ingress_identity)
        if identity is None:
            return
        eligible = self.db.execute(
            """SELECT 1 FROM provider_jobs job WHERE job.job_id=?
               AND job.status='queued' AND job.attempt_count=0 AND job.lease_token IS NULL
               AND job.lease_owner IS NULL AND job.lease_expires_at IS NULL
               AND job.provider_started_at IS NULL
               AND NOT EXISTS(SELECT 1 FROM provider_job_inputs input WHERE input.job_id=job.job_id)
               AND NOT EXISTS(SELECT 1 FROM provider_execution_checkpoints p WHERE p.job_id=job.job_id)
               AND NOT EXISTS(SELECT 1 FROM provider_job_telegram_ingress i WHERE i.job_id=job.job_id)
               """,
            (job_id,),
        ).fetchone()
        if eligible is None:
            raise StateError("Telegram ingress requires fresh admission before first input")
        self.db.execute(
            "INSERT INTO provider_job_telegram_ingress VALUES (?,?)", (job_id, identity)
        )

    def record_fresh_target_in_transaction(self, job_id: str) -> None:
        """Called only immediately after the first coherent control INSERT.

        Unknown provenance preserves ordinary Stage2 acceptance. The owning
        branch and SQL guard prevent repeat recording from upgrading old work.
        """
        if not self.db.in_transaction:
            raise StateError("Telegram target requires its checkpoint owner transaction")
        identity = self.identity(job_id)
        if identity is None:
            return
        self.db.execute(
            "INSERT INTO codex_telegram_precaution_targets VALUES (?,?)", (job_id, identity)
        )
