"""Fenced, passive approvals observed while native turn submission is pending."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .approval_observations import (
    IDENTITY,
    activity_metadata_count,
    approval_event_key,
    approval_metadata,
    approval_notice_html,
)
from .codex_activity import CodexActivityEvent
from .preacceptance_binding import PREPARED_BINDING, current_prepared_binding, epoch_is_current
from .state_errors import StateError

if TYPE_CHECKING:
    from .state import HubState
    from .task_activity import TaskActivityState

MAX_EARLY_REQUESTS = 128
MAX_COMBINED_METADATA = 512


@dataclass(frozen=True, slots=True)
class RuntimeEpoch:
    slot_key: str
    agent_id: str
    epoch: int
    instance_token: str


class PreacceptanceApprovalState:
    def __init__(self, state: HubState, *, notices_enabled: bool = False) -> None:
        self.state = state
        self.db = state._connection
        self.notices_enabled = notices_enabled

    @staticmethod
    def _time(now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise StateError("early approval timestamp must be timezone-aware")
        return now.astimezone(timezone.utc).isoformat()

    def _runtime_current(self, runtime: RuntimeEpoch) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM preacceptance_runtime_epochs WHERE slot_key=? AND agent_id=? "
                "AND epoch=? AND instance_token=?",
                (runtime.slot_key, runtime.agent_id, runtime.epoch, runtime.instance_token),
            ).fetchone()
            is not None
        )

    def _supersede_notice(self, event_key: str, timestamp: str) -> None:
        self.db.execute(
            "UPDATE task_lifecycle_notices SET status='superseded',updated_at=? "
            "WHERE event_key=? AND status IN ('pending','leased') AND attempt_count=0 "
            "AND send_started_at IS NULL",
            (timestamp, event_key),
        )

    def _retire(self, scope_id: str, timestamp: str) -> None:
        row = self.db.execute(
            "SELECT state FROM preacceptance_scopes WHERE scope_id=?", (scope_id,)
        ).fetchone()
        if row is None or row["state"] != "open":
            return
        for request in self.db.execute(
            "SELECT event_key FROM preacceptance_requests WHERE scope_id=?", (scope_id,)
        ).fetchall():
            self._supersede_notice(request["event_key"], timestamp)
        self.db.execute(
            "UPDATE preacceptance_requests SET state='retired' WHERE scope_id=?", (scope_id,)
        )
        self.db.execute(
            "UPDATE preacceptance_scopes SET state='retired',closed_at=? WHERE scope_id=?",
            (timestamp, scope_id),
        )

    def register_runtime(
        self,
        *,
        agent_id: str,
        worker_slot: int,
        instance_token: str,
        now: datetime,
    ) -> RuntimeEpoch:
        if (
            not isinstance(agent_id, str)
            or not IDENTITY.fullmatch(agent_id)
            or not isinstance(instance_token, str)
            or not IDENTITY.fullmatch(instance_token)
            or not isinstance(worker_slot, int)
            or isinstance(worker_slot, bool)
            or not 1 <= worker_slot <= 16
        ):
            raise StateError("invalid early approval runtime identity")
        slot_key = json.dumps(["external-worker", agent_id, worker_slot], separators=(",", ":"))
        timestamp = self._time(now)
        with self.state._immediate_transaction():
            previous = self.db.execute(
                "SELECT * FROM preacceptance_runtime_epochs WHERE slot_key=?", (slot_key,)
            ).fetchone()
            if previous is not None and previous["instance_token"] == instance_token:
                return RuntimeEpoch(slot_key, agent_id, previous["epoch"], instance_token)
            for old in self.db.execute(
                "SELECT scope_id FROM preacceptance_scopes WHERE slot_key=? AND state='open'",
                (slot_key,),
            ).fetchall():
                self._retire(old["scope_id"], timestamp)
            epoch = 1 if previous is None else previous["epoch"] + 1
            self.db.execute(
                "INSERT INTO preacceptance_runtime_epochs VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(slot_key) DO UPDATE SET epoch=excluded.epoch,"
                "instance_token=excluded.instance_token,registered_at=excluded.registered_at",
                (slot_key, agent_id, worker_slot, epoch, instance_token, timestamp),
            )
            return RuntimeEpoch(slot_key, agent_id, epoch, instance_token)

    def open_scope(
        self,
        job_id: str,
        token: str,
        runtime: RuntimeEpoch,
        *,
        provider_thread_id: str,
        project_root: str,
        now: datetime,
    ) -> str | None:
        timestamp = self._time(now)
        with self.state._immediate_transaction():
            if not self._runtime_current(runtime):
                return None
            live = current_prepared_binding(
                self.db, job_id, token, timestamp, lambda: self.state.codex_permission_profile
            )
            if live is None or (
                live["agent_id"] != runtime.agent_id
                or live["provider_turn_id"] is not None
                or (live["provider_thread_id"], live["project_root"])
                != (provider_thread_id, project_root)
            ):
                raise StateError("early approval requires the exact prepared execution")
            previous = self.db.execute(
                "SELECT * FROM preacceptance_scopes WHERE job_id=? AND lease_token=?",
                (job_id, token),
            ).fetchone()
            if previous is not None:
                return (
                    previous["scope_id"]
                    if (
                        previous["state"] == "open"
                        and epoch_is_current(self.db, previous)
                        and previous["slot_key"] == runtime.slot_key
                        and all(previous[key] == live[key] for key in PREPARED_BINDING)
                    )
                    else None
                )
            scope_id = str(uuid.uuid4())
            columns = (
                "scope_id",
                "slot_key",
                "epoch",
                "instance_token",
                "job_id",
                *PREPARED_BINDING,
                "state",
                "created_at",
            )
            values = (
                scope_id,
                runtime.slot_key,
                runtime.epoch,
                runtime.instance_token,
                job_id,
                *(live[key] for key in PREPARED_BINDING),
                "open",
                timestamp,
            )
            self.db.execute(
                "INSERT INTO preacceptance_scopes ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in values)
                + ")",
                values,
            )
            return scope_id

    def _bound(self, scope_id: str | None, runtime: RuntimeEpoch, timestamp: str):
        row = self.db.execute(
            "SELECT * FROM preacceptance_scopes WHERE scope_id=?", (scope_id,)
        ).fetchone()
        if (
            row is None
            or row["state"] != "open"
            or (row["slot_key"], row["epoch"], row["instance_token"], row["agent_id"])
            != (runtime.slot_key, runtime.epoch, runtime.instance_token, runtime.agent_id)
        ):
            return None
        live = current_prepared_binding(
            self.db,
            row["job_id"],
            row["lease_token"],
            timestamp,
            lambda: self.state.codex_permission_profile,
        )
        if (
            not epoch_is_current(self.db, row)
            or live is None
            or any(row[key] != live[key] for key in PREPARED_BINDING)
        ):
            self._retire(row["scope_id"], timestamp)
            return None
        return row, live

    def observe(
        self,
        scope_id: str | None,
        runtime: RuntimeEpoch,
        event: CodexActivityEvent,
        *,
        now: datetime,
    ) -> bool:
        timestamp = self._time(now)
        with self.state._immediate_transaction():
            binding = self._bound(scope_id, runtime, timestamp)
            if binding is None:
                return False
            scope, live = binding
            if live["provider_turn_id"] is not None:
                return False
            if event.thread_id != scope["provider_thread_id"]:
                return False
            try:
                identity, item = approval_metadata(event)
            except ValueError as exc:
                raise StateError(str(exc)) from exc
            prior = self.db.execute(
                "SELECT * FROM preacceptance_requests WHERE scope_id=? AND identity=?",
                (scope_id, identity),
            ).fetchone()
            if prior is not None and (
                prior["observed_turn_id"],
                prior["item_identity"],
                prior["category"],
            ) != (event.turn_id, item, event.category):
                raise StateError("early approval request identity changed")
            if prior is not None and (
                prior["state"] != "pending" or event.kind == "approval_requested"
            ):
                return False
            event_key = approval_event_key(scope["job_id"], identity)
            if event.kind == "approval_resolved":
                if prior is None:
                    return False
                self.db.execute(
                    "UPDATE preacceptance_requests SET state='resolved',resolved_at=? "
                    "WHERE scope_id=? AND identity=?",
                    (timestamp, scope_id, identity),
                )
                self._supersede_notice(event_key, timestamp)
                return True
            count = self.db.execute(
                "SELECT count(*) FROM preacceptance_requests WHERE scope_id=?", (scope_id,)
            ).fetchone()[0]
            if (
                count >= MAX_EARLY_REQUESTS
                or activity_metadata_count(self.db, scope["job_id"]) >= MAX_COMBINED_METADATA
            ):
                return False
            self.db.execute(
                "INSERT INTO preacceptance_requests VALUES(?,?,?,?,?,'pending',?,?,NULL)",
                (scope_id, identity, event.turn_id, item, event.category, event_key, timestamp),
            )
            if self.notices_enabled:
                self.state.task_notices.prepare_notice_in_transaction(
                    event_key=event_key,
                    kind="approval_wait",
                    job_id=scope["job_id"],
                    chat_id=scope["chat_id"],
                    thread_id=scope["thread_id"],
                    telegram_html=approval_notice_html(event.category),
                    now=now,
                )
            return True

    def promote_in_transaction(
        self,
        scope_id: str | None,
        runtime: RuntimeEpoch,
        activity: TaskActivityState,
        *,
        thread_id: str,
        turn_id: str,
        now: datetime,
    ) -> bool:
        if not self.db.in_transaction:
            raise StateError("early approval promotion requires an active transaction")
        timestamp = self._time(now)
        binding = self._bound(scope_id, runtime, timestamp)
        if binding is None:
            return False
        scope, live = binding
        if (live["provider_thread_id"], live["provider_turn_id"]) != (thread_id, turn_id):
            raise StateError("early approval promotion has no exact accepted checkpoint")
        for request in self.db.execute(
            "SELECT * FROM preacceptance_requests WHERE scope_id=?", (scope_id,)
        ).fetchall():
            if request["observed_turn_id"] == turn_id and activity.import_approval_in_transaction(
                scope["job_id"],
                scope["lease_token"],
                thread_id=thread_id,
                turn_id=turn_id,
                identity=request["identity"],
                item_identity=request["item_identity"],
                category=request["category"],
                state=request["state"],
                now=now,
            ):
                continue
            self.db.execute(
                "UPDATE preacceptance_requests SET state='retired' WHERE scope_id=? AND identity=?",
                (scope_id, request["identity"]),
            )
            self._supersede_notice(request["event_key"], timestamp)
        self.db.execute(
            "UPDATE preacceptance_scopes SET state='promoted',closed_at=? WHERE scope_id=?",
            (timestamp, scope_id),
        )
        return True

    def retire(self, scope_id: str | None, runtime: RuntimeEpoch, *, now: datetime) -> None:
        with self.state._immediate_transaction():
            row = self.db.execute(
                "SELECT * FROM preacceptance_scopes WHERE scope_id=? AND slot_key=? "
                "AND epoch=? AND instance_token=?",
                (scope_id, runtime.slot_key, runtime.epoch, runtime.instance_token),
            ).fetchone()
            if row is not None:
                self._retire(row["scope_id"], self._time(now))
