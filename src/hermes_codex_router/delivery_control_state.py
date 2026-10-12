"""Delivery snapshots and exact local consent on a caller-owned transaction."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .delivery_control_predicates import final_control_reconciled, progress_control_reconciled
from .state_errors import StateError

ACTION = "reconcile_delivery_control_wait"
# Existing final/progress delivery policy, not provider invocation attempts.
EXHAUSTED_DELIVERY_ATTEMPTS = 20
TARGETS = {
    "final_outbox": ("telegram_outbox", "outbox_id"),
    "progress_delivery": ("provider_progress_deliveries", "progress_id"),
}


@dataclass(frozen=True, slots=True)
class DeliveryControlPreview:
    target_kind: str
    target_id: str
    job_id: str
    result_id: str | None
    chat_id: int
    thread_id: int
    delivery_status: str
    snapshot: str
    part_count: int
    receipted_parts: int
    disposition_snapshot: str | None
    binding_matches: bool
    capability: str = "local_owner_reconciliation"
    apply_available: bool = True
    control_effect: str = "outstanding"


@dataclass(frozen=True, slots=True)
class DeliveryControlDisposition:
    disposition_id: str
    target_kind: str
    target_id: str
    job_id: str
    snapshot: str
    authority: str
    applied_at: str
    delivery_status: str
    control_effect: str


def _canonical_root(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and value.startswith("/")
        and not value.startswith("//")
        and "\x00" not in value
        and ".." not in value.split("/")
        and str(PurePosixPath(value)) == value
    )


class DeliveryControlState:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def _job_row(self, table: str, job_id: str) -> dict[str, Any] | None:
        row = self.db.execute(f"SELECT * FROM {table} WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _terminal_matches(
        evidence: dict[str, Any] | None, checkpoint: dict[str, Any] | None
    ) -> bool:
        return bool(
            evidence is not None
            and checkpoint is not None
            and evidence["job_id"] == checkpoint["job_id"]
            and evidence["terminal_status"] in ("completed", "failed", "interrupted")
            and all(
                isinstance(checkpoint[key], str) and 1 <= len(checkpoint[key]) <= 256
                for key in ("provider_thread_id", "provider_turn_id")
            )
            and _canonical_root(checkpoint["project_root"])
            and all(
                evidence[key] == checkpoint[key]
                for key in ("provider_thread_id", "provider_turn_id", "project_root")
            )
        )

    def _load_target(self, kind: str, target_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.db.in_transaction:
            raise StateError("delivery control preview requires a state-owned transaction")
        if (
            not isinstance(kind, str)
            or kind not in TARGETS
            or not isinstance(target_id, str)
            or not 1 <= len(target_id) <= 128
        ):
            raise StateError("invalid delivery control target")
        table, key = TARGETS[kind]
        row = self.db.execute(f"SELECT * FROM {table} WHERE {key}=?", (target_id,)).fetchone()
        if (
            row is None
            or row["status"] not in ("unknown", "failed")
            or (row["status"] == "failed" and row["attempt_count"] != EXHAUSTED_DELIVERY_ATTEMPTS)
            or any(row[k] is not None for k in ("lease_owner", "lease_token", "lease_expires_at"))
        ):
            raise StateError(
                "delivery control target must be parked unknown or exhausted failed without a sender lease"
            )
        target = dict(row)
        job = self._job_row("provider_jobs", target["job_id"])
        if job is None:
            raise StateError("delivery control job is unavailable")
        topic = self.db.execute(
            "SELECT * FROM topics WHERE topic_id=?", (job["topic_id"],)
        ).fetchone()
        session = self.db.execute(
            "SELECT * FROM agent_sessions WHERE session_id=?", (job["session_id"],)
        ).fetchone()
        if (
            topic is None
            or session is None
            or target["sender_agent_id"] != job["agent_id"]
            or target["chat_id"] != job["chat_id"]
            or target["chat_id"] != topic["chat_id"]
            or target["thread_id"] != topic["thread_id"]
            or session["topic_id"] != job["topic_id"]
            or session["agent_id"] != job["agent_id"]
            or session["generation"] != job["session_generation"]
        ):
            raise StateError("delivery control target binding is inconsistent")
        scope = topic["execution_scope"]
        if (
            not isinstance(scope, str)
            or not scope.startswith("root:")
            or not _canonical_root(scope[5:])
        ):
            raise StateError("delivery control requires an established canonical execution scope")
        result = (
            self._job_row("provider_job_results", job["job_id"]) if kind == "final_outbox" else None
        )
        item = None
        parts: list[dict[str, Any]] = []
        if kind == "final_outbox":
            if job["status"] not in ("result_ready", "failed", "cancelled", "indeterminate") or (
                job["status"] == "result_ready" and result is None
            ):
                raise StateError("delivery control final job or saved result is inconsistent")
            parts = [
                dict(p)
                for p in self.db.execute(
                    "SELECT * FROM telegram_outbox_parts WHERE outbox_id=? ORDER BY part_index",
                    (target_id,),
                )
            ]
        else:
            item_row = self.db.execute(
                "SELECT * FROM provider_visible_items WHERE sequence=?", (target["item_sequence"],)
            ).fetchone()
            if (
                item_row is None
                or item_row["job_id"] != job["job_id"]
                or item_row["phase"] != "commentary"
            ):
                raise StateError("delivery control progress item is inconsistent")
            item = dict(item_row)
        checkpoint = self._job_row("provider_execution_checkpoints", job["job_id"])
        evidence = self._job_row("provider_turn_terminal_evidence", job["job_id"])
        resolution = self._job_row("provider_job_resolutions", job["job_id"])
        if (
            kind == "final_outbox"
            and target["status"] == "failed"
            and job["status"] == "indeterminate"
        ):
            resolved = resolution is not None and resolution["resolution"] in (
                "acknowledged",
                "superseded",
                "externally_completed",
            )
            if not resolved and not self._terminal_matches(evidence, checkpoint):
                raise StateError(
                    "failed uncertain notice requires existing exact terminal evidence or owner resolution"
                )
        binding = dict(
            target_kind=kind,
            outbox_id=target_id if kind == "final_outbox" else None,
            progress_id=target_id if kind == "progress_delivery" else None,
            item_sequence=item["sequence"] if item else None,
            job_id=job["job_id"],
            result_id=result["result_id"] if result else None,
            topic_id=job["topic_id"],
            topic_sequence=job["topic_sequence"],
            project_id=topic["project_id"],
            session_id=job["session_id"],
            session_generation=job["session_generation"],
            sender_agent_id=target["sender_agent_id"],
            chat_id=target["chat_id"],
            thread_id=target["thread_id"],
        )
        origin = self.db.execute(
            "SELECT * FROM codex_session_origins WHERE session_id=?", (job["session_id"],)
        ).fetchone()
        snapshot = dict(
            action=ACTION,
            version=1,
            binding=binding,
            execution_scope_at_preview=scope,
            target=target,
            job=job,
            result=result,
            parts=parts,
            item=item,
            checkpoint=checkpoint,
            terminal_evidence=evidence,
            resolution=resolution,
            origin=dict(origin) if origin is not None else None,
        )
        return binding, snapshot

    @staticmethod
    def _digest(snapshot: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    def preview(self, kind: str, target_id: str) -> DeliveryControlPreview:
        binding, snapshot = self._load_target(kind, target_id)
        key = TARGETS[kind][1]
        prior = self.db.execute(
            f"SELECT * FROM telegram_delivery_control_dispositions WHERE {key}=?", (target_id,)
        ).fetchone()
        binding_matches = prior is not None and all(prior[k] == v for k, v in binding.items())
        return DeliveryControlPreview(
            target_kind=kind,
            target_id=target_id,
            job_id=binding["job_id"],
            result_id=binding["result_id"],
            chat_id=binding["chat_id"],
            thread_id=binding["thread_id"],
            delivery_status=snapshot["target"]["status"],
            snapshot=self._digest(snapshot),
            part_count=len(snapshot["parts"]),
            receipted_parts=sum(p["telegram_message_id"] is not None for p in snapshot["parts"]),
            disposition_snapshot=prior["snapshot"] if prior is not None else None,
            binding_matches=binding_matches,
            control_effect=self._record(prior).control_effect
            if prior is not None
            else "outstanding",
        )

    def apply_in_transaction(
        self, kind: str, target_id: str, expected_snapshot: str, consent: bool, applied_at: str
    ) -> DeliveryControlDisposition:
        if not self.db.in_transaction:
            raise StateError("delivery control apply requires a state-owned transaction")
        if consent is not True:
            raise StateError("explicit agreement to accept unconfirmed delivery is required")
        if (
            not isinstance(expected_snapshot, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_snapshot) is None
        ):
            raise StateError("exact delivery control preview snapshot is required")
        if (
            not isinstance(kind, str)
            or kind not in TARGETS
            or not isinstance(target_id, str)
            or not 1 <= len(target_id) <= 128
        ):
            raise StateError("invalid delivery control target")
        key = TARGETS[kind][1]
        prior = self.db.execute(
            f"SELECT * FROM telegram_delivery_control_dispositions WHERE {key}=?", (target_id,)
        ).fetchone()
        if prior is not None:
            if prior["snapshot"] != expected_snapshot:
                raise StateError("delivery control already has a different immutable disposition")
            return self._record(prior)
        binding, snapshot = self._load_target(kind, target_id)
        if self._digest(snapshot) != expected_snapshot:
            raise StateError("delivery control preview is stale; inspect a fresh preview")
        terminal = resolution = None
        if (
            kind == "final_outbox"
            and snapshot["target"]["status"] == "failed"
            and snapshot["job"]["status"] == "indeterminate"
        ):
            proof = snapshot["resolution"]
            if proof is not None and proof["resolution"] in (
                "acknowledged",
                "superseded",
                "externally_completed",
            ):
                resolution = binding["job_id"]
            else:
                terminal = binding["job_id"]
        values = dict(
            disposition_id=str(uuid.uuid4()),
            **binding,
            execution_scope_at_consent=snapshot["execution_scope_at_preview"],
            delivery_status_at_consent=snapshot["target"]["status"],
            action=ACTION,
            snapshot_version=1,
            snapshot=expected_snapshot,
            authority="local_owner_cli",
            applied_at=applied_at,
            terminal_evidence_job_id=terminal,
            resolution_job_id=resolution,
        )
        self.db.execute(
            f"INSERT INTO telegram_delivery_control_dispositions ({','.join(values)}) "
            f"VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
        row = self.db.execute(
            "SELECT * FROM telegram_delivery_control_dispositions WHERE disposition_id=?",
            (values["disposition_id"],),
        ).fetchone()
        assert row is not None
        return self._record(row)

    def _record(self, row: sqlite3.Row) -> DeliveryControlDisposition:
        kind = row["target_kind"]
        table, key = TARGETS[kind]
        predicate = (
            final_control_reconciled if kind == "final_outbox" else progress_control_reconciled
        )
        target = self.db.execute(
            f"SELECT status,{predicate('target')} AS reconciled FROM {table} target WHERE {key}=?",
            (row[key],),
        ).fetchone()
        return DeliveryControlDisposition(
            disposition_id=row["disposition_id"],
            target_kind=kind,
            target_id=row[key],
            job_id=row["job_id"],
            snapshot=row["snapshot"],
            authority=row["authority"],
            applied_at=row["applied_at"],
            delivery_status=target["status"] if target is not None else "missing",
            control_effect="delivery_wait_reconciled"
            if target is not None and target["reconciled"]
            else "disposition_binding_changed",
        )
