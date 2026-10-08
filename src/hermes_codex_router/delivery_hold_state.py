"""One exact owner disposition; reads/inserts only inside HubState-owned transactions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .delivery_hold_predicates import outbox_delivery_hold_released

ACTION = "continue_without_confirmed_delivery"


@dataclass(frozen=True, slots=True)
class DeliveryHoldDisposition:
    outbox_id: str
    job_id: str
    snapshot: str
    authority: str
    applied_at: str
    hold_status: str


@dataclass(frozen=True, slots=True)
class DeliveryHoldPreview:
    outbox_id: str
    job_id: str
    result_id: str | None
    chat_id: int
    thread_id: int
    snapshot: str
    hold_status: str
    part_count: int
    receipted_parts: int
    disposition_snapshot: str | None = None
    control_consequences: tuple[str, ...] = ()
    remaining_boundaries: tuple[str, ...] = (
        "native_execution_uncertainty",
        "root_writer_exclusion",
        "stop_and_owner_holds",
        "session_and_writer_controls",
    )


class DeliveryHoldState:
    def __init__(self, db: sqlite3.Connection, error: Callable[[str], Exception]) -> None:
        self.db = db
        self.error = error

    def _target(self, outbox_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.db.in_transaction:
            raise self.error("delivery hold requires a state-owned snapshot transaction")
        if not isinstance(outbox_id, str) or not 1 <= len(outbox_id) <= 128:
            raise self.error("invalid delivery hold target")
        row = self.db.execute(
            "SELECT * FROM telegram_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if (
            row is None
            or row["status"] != "unknown"
            or any(row[k] is not None for k in ("lease_owner", "lease_token", "lease_expires_at"))
        ):
            raise self.error("delivery hold target must be parked unknown with no sender lease")
        outbox = dict(row)
        job = self.db.execute(
            "SELECT * FROM provider_jobs WHERE job_id=?", (row["job_id"],)
        ).fetchone()
        if job is None:
            raise self.error("delivery hold target job is unavailable")
        topic = self.db.execute(
            "SELECT * FROM topics WHERE topic_id=?", (job["topic_id"],)
        ).fetchone()
        session = self.db.execute(
            "SELECT * FROM agent_sessions WHERE session_id=?", (job["session_id"],)
        ).fetchone()
        result = self.db.execute(
            "SELECT * FROM provider_job_results WHERE job_id=?", (job["job_id"],)
        ).fetchone()
        if (
            topic is None
            or session is None
            or job["status"] not in ("result_ready", "failed", "cancelled", "indeterminate")
            or (job["status"] == "result_ready" and result is None)
            or outbox["sender_agent_id"] != job["agent_id"]
            or outbox["chat_id"] != job["chat_id"]
            or outbox["chat_id"] != topic["chat_id"]
            or outbox["thread_id"] != topic["thread_id"]
            or session["topic_id"] != job["topic_id"]
            or session["agent_id"] != job["agent_id"]
            or session["generation"] != job["session_generation"]
        ):
            raise self.error("delivery hold target binding or saved result is inconsistent")
        if not isinstance(topic["execution_scope"], str) or not topic["execution_scope"].startswith(
            "root:/"
        ):
            raise self.error("delivery hold requires an established canonical execution scope")
        binding = dict(
            outbox_id=outbox_id,
            job_id=job["job_id"],
            result_id=result["result_id"] if result else None,
            topic_id=topic["topic_id"],
            topic_sequence=job["topic_sequence"],
            project_id=topic["project_id"],
            execution_scope=topic["execution_scope"],
            session_id=job["session_id"],
            session_generation=job["session_generation"],
            sender_agent_id=outbox["sender_agent_id"],
            chat_id=outbox["chat_id"],
            thread_id=outbox["thread_id"],
        )
        # Hash every ordered part, including content/artifact/receipt provenance; output is bounded.
        parts = [
            dict(p)
            for p in self.db.execute(
                "SELECT * FROM telegram_outbox_parts WHERE outbox_id=? ORDER BY part_index",
                (outbox_id,),
            )
        ]
        snapshot = dict(
            action=ACTION,
            version=1,
            binding=binding,
            outbox=outbox,
            job=dict(job),
            result=dict(result) if result else None,
            session_binding={
                key: session[key] for key in ("session_id", "topic_id", "agent_id", "generation")
            },
            parts=parts,
        )
        return binding, snapshot

    @staticmethod
    def _digest(snapshot: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    def preview(self, outbox_id: str) -> DeliveryHoldPreview:
        binding, target = self._target(outbox_id)
        prior = self.db.execute(
            "SELECT * FROM telegram_delivery_hold_dispositions WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        released = bool(
            self.db.execute(
                f"SELECT {outbox_delivery_hold_released('o')} FROM telegram_outbox o WHERE o.outbox_id=?",
                (outbox_id,),
            ).fetchone()[0]
        )
        return DeliveryHoldPreview(
            outbox_id=outbox_id,
            job_id=binding["job_id"],
            result_id=binding["result_id"],
            chat_id=binding["chat_id"],
            thread_id=binding["thread_id"],
            snapshot=self._digest(target),
            hold_status=(
                "released_by_owner"
                if released
                else "disposition_binding_changed"
                if prior is not None
                else "outstanding"
            ),
            part_count=len(target["parts"]),
            receipted_parts=sum(p["telegram_message_id"] is not None for p in target["parts"]),
            disposition_snapshot=prior["snapshot"] if prior is not None else None,
            control_consequences=("topic_binding_retained_for_disposition_lifetime",)
            + (
                (
                    "result_ready_remains_without_time_limit",
                    "topic_new_model_agent_local_return_remain_blocked",
                    "scope_wide_local_terminal_transfer_remains_blocked",
                    "agent_managed_externally_drain_remains_blocked",
                )
                if target["job"]["status"] == "result_ready"
                else ()
            ),
        )

    def release(
        self, outbox_id: str, expected_snapshot: str, consent: bool, applied_at: str
    ) -> DeliveryHoldDisposition:
        if consent is not True:
            raise self.error(
                "explicit agreement to continue without confirmed delivery is required"
            )
        if (
            not isinstance(expected_snapshot, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_snapshot) is None
        ):
            raise self.error("exact preview snapshot is required")
        if not self.db.in_transaction:
            raise self.error("delivery hold release requires a state-owned transaction")
        prior = self.db.execute(
            "SELECT * FROM telegram_delivery_hold_dispositions WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if prior is not None:
            if prior["snapshot"] != expected_snapshot:
                raise self.error("delivery hold already has a different immutable disposition")
            return self._record(prior)
        binding, target = self._target(outbox_id)
        if self._digest(target) != expected_snapshot:
            raise self.error("delivery hold preview is stale; inspect a fresh preview")
        values = dict(
            **binding,
            action=ACTION,
            snapshot_version=1,
            snapshot=expected_snapshot,
            authority="local_owner_cli",
            applied_at=applied_at,
        )
        self.db.execute(
            f"INSERT INTO telegram_delivery_hold_dispositions ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
        row = self.db.execute(
            "SELECT * FROM telegram_delivery_hold_dispositions WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        assert row is not None
        return self._record(row)

    def _record(self, row: sqlite3.Row) -> DeliveryHoldDisposition:
        effect = self.db.execute(
            f"SELECT {outbox_delivery_hold_released('o')} FROM telegram_outbox o WHERE o.outbox_id=?",
            (row["outbox_id"],),
        ).fetchone()
        return DeliveryHoldDisposition(
            outbox_id=row["outbox_id"],
            job_id=row["job_id"],
            snapshot=row["snapshot"],
            authority=row["authority"],
            applied_at=row["applied_at"],
            hold_status="released_by_owner"
            if effect is not None and effect[0]
            else "disposition_binding_changed",
        )
