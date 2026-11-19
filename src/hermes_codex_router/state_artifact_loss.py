"""HubState transaction facade for local artifact-loss attestations.

The durable ledger lives in :mod:`artifact_loss_state`; this small facade keeps
the public HubState entry points out of the already-full aggregate state class.
It owns no separate connection or transaction boundary.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol, cast

from .artifact_loss_state import (
    ArtifactLossPreview,
    ArtifactLossRecord,
    ArtifactLossState,
    ArtifactLossTarget,
)
from .state_errors import StateError


class _ArtifactLossStateOwner(Protocol):
    _connection: sqlite3.Connection

    def _immediate_transaction(self) -> AbstractContextManager[None]: ...


class ArtifactLossHubStateFacade:
    """Public local-only loss methods mixed into the aggregate HubState."""

    def _artifact_loss_owner(self) -> _ArtifactLossStateOwner:
        return cast(_ArtifactLossStateOwner, self)

    def preview_artifact_loss(
        self,
        targets: Iterable[ArtifactLossTarget],
        *,
        spool_root: Path,
        recovery_audit_sha256: str,
        operation_id: str | None = None,
    ) -> ArtifactLossPreview:
        """Read one exact local-loss declaration preview without changing Hub state."""
        owner = self._artifact_loss_owner()
        if owner._connection.in_transaction:
            raise StateError("cannot nest an artifact loss preview transaction")
        owner._connection.execute("BEGIN")
        try:
            preview = ArtifactLossState(owner._connection, StateError).preview(
                targets,
                spool_root=spool_root,
                recovery_audit_sha256=recovery_audit_sha256,
                operation_id=operation_id,
            )
            owner._connection.commit()
            return preview
        except BaseException:
            owner._connection.rollback()
            raise

    def declare_artifact_loss(
        self,
        targets: Iterable[ArtifactLossTarget],
        *,
        spool_root: Path,
        recovery_audit_sha256: str,
        expected_snapshot: str,
        operation_id: str,
        acknowledge_irrecoverable_loss: bool,
        now: datetime | None = None,
    ) -> ArtifactLossRecord:
        """Append a local-owner loss attestation; queue and delivery state remain untouched."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise StateError("artifact loss time must be timezone-aware")
        owner = self._artifact_loss_owner()
        with owner._immediate_transaction():
            return ArtifactLossState(owner._connection, StateError).apply(
                targets,
                spool_root=spool_root,
                recovery_audit_sha256=recovery_audit_sha256,
                expected_snapshot=expected_snapshot,
                operation_id=operation_id,
                acknowledge_irrecoverable_loss=acknowledge_irrecoverable_loss,
                applied_at=current.astimezone(timezone.utc).isoformat(),
            )
