"""Transactions for human permission receipts; never an approval authority."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .claude_permission_binding import binding_digest, binding_snapshot
from .claude_permission_protocol import ProtectedPayload, canonical_uuid
from .execution_journal import ExecutionJournal
from .state import HubState, StateError

FILE_TOOLS = frozenset({"Read", "Glob", "Grep", "Write", "Edit"})


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PermissionLaunch:
    epoch: str
    job_id: str
    lease_id: str
    session_id: str
    root: Path
    generation: int
    binding_digest: str


class ClaudePermissionJournal:
    """Own every permission transaction on the caller's dedicated connection."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection
        self.execution = ExecutionJournal(state)

    def _binding(self, job_id: str, token: str, session_id: str, root: Path) -> tuple[str, int]:
        checkpoint = self.execution._claude_checkpoint(job_id, token, session_id, root)
        if checkpoint["completed_text"] is not None:
            raise StateError("Claude permission launch is complete")
        row = binding_snapshot(self.connection, job_id)
        if row is None or row[8] is None:
            raise StateError("Claude permission scope changed")
        stop = self.connection.execute(
            "SELECT 1 FROM provider_stop_requests WHERE topic_id=? AND "
            "(status='pending' OR created_at>=?) LIMIT 1",
            (row[0], row[8]),
        ).fetchone()
        if stop is not None:
            raise StateError("Claude permission launch was stopped")
        return binding_digest(job_id, token, session_id, str(root), row), int(row[4])

    def open_launch(self, job_id: str, token: str, session_id: str, root: Path) -> PermissionLaunch:
        canonical_uuid(token)
        root = root.resolve(strict=True)
        with self.state._immediate_transaction():
            digest, generation = self._binding(job_id, token, session_id, root)
            epoch = str(uuid4())
            try:
                self.connection.execute(
                    "INSERT INTO claude_permission_launches VALUES (?,?,?,'active',?)",
                    (epoch, job_id, digest, datetime.now(timezone.utc).isoformat()),
                )
            except sqlite3.IntegrityError:
                raise StateError("Claude permission launch cannot be replayed") from None
        return PermissionLaunch(epoch, job_id, token, session_id, root, generation, digest)

    def bind_session_mode(
        self,
        job_id: str,
        token: str,
        native: str,
        root: Path,
        *,
        mode: str,
        home: Path,
        is_new: bool,
    ) -> None:
        if mode not in {"text_only", "file_tools"}:
            raise StateError("unsupported Claude session mode")
        home_digest = _digest(str(home.resolve()))
        with self.state._immediate_transaction():
            self._binding(job_id, token, native, root)
            row = self.connection.execute(
                "SELECT mode,home_digest FROM claude_permission_session_modes WHERE provider_session_id=?",
                (native,),
            ).fetchone()
            if row is None:
                if not is_new and mode != "text_only":
                    raise StateError("legacy Claude session cannot change mode")
                self.connection.execute(
                    "INSERT INTO claude_permission_session_modes VALUES (?,?,?)",
                    (native, mode, home_digest),
                )
            elif tuple(row) != (mode, home_digest):
                raise StateError("Claude session mode or home changed")

    def _current(self, launch: PermissionLaunch) -> None:
        digest, generation = self._binding(
            launch.job_id, launch.lease_id, launch.session_id, launch.root
        )
        row = self.connection.execute(
            "SELECT binding_digest,status FROM claude_permission_launches WHERE launch_epoch=? "
            "AND job_id=?",
            (launch.epoch, launch.job_id),
        ).fetchone()
        if (
            row is None
            or tuple(row) != (digest, "active")
            or digest != launch.binding_digest
            or generation != launch.generation
        ):
            raise StateError("Claude permission launch binding changed")

    def prepare(
        self,
        launch: PermissionLaunch,
        nonce: str,
        event_digest: str,
        tool: str,
        tool_input: Any,
        *,
        lifetime_seconds: int = 120,
    ) -> str:
        if (
            tool not in FILE_TOOLS
            or type(lifetime_seconds) is not int
            or not 1 <= lifetime_seconds <= 120
        ):
            raise StateError("Claude permission tool or deadline is unsupported")
        if len(event_digest) != 64 or any(c not in "0123456789abcdef" for c in event_digest):
            raise StateError("Claude permission event digest is invalid")
        expires_at = int(time.time() * 1000) + lifetime_seconds * 1000
        payload = ProtectedPayload(
            nonce,
            launch.job_id,
            launch.session_id,
            launch.generation,
            _digest(str(launch.root)),
            launch.lease_id,
            launch.epoch,
            tool,
            tool_input,
            expires_at,
        ).to_json()
        with self.state._immediate_transaction():
            self._current(launch)
            count = self.connection.execute(
                "SELECT count(*) FROM claude_permission_requests WHERE launch_epoch=?",
                (launch.epoch,),
            ).fetchone()[0]
            if count >= 128:
                raise StateError("Claude permission request limit reached")
            try:
                self.connection.execute(
                    "INSERT INTO claude_permission_requests VALUES (?,?,?,?,?,'pending',NULL)",
                    (nonce, launch.epoch, _digest(payload), event_digest, expires_at),
                )
            except sqlite3.IntegrityError:
                raise StateError("Claude permission nonce was already used") from None
            job = self.state.get_provider_job(launch.job_id)
            topic = self.state.get_topic(job.topic_id)
            self.state.task_notices.prepare_notice_in_transaction(
                event_key=f"claude-permission:{nonce}",
                kind="claude_permission_wait",
                job_id=job.job_id,
                chat_id=topic.chat_id,
                thread_id=topic.thread_id,
                telegram_html="Claude ждёт разрешения на файловую операцию. Откройте запрос в Agent Session Remote и выберите Allow или Deny. /stop остановит задачу. Если решение не поступит, операция будет отклонена.",
                now=datetime.now(timezone.utc),
            )
        return payload

    def consume(self, launch: PermissionLaunch, payload: str, decision: str) -> None:
        """Called only after protected transport authentication; commit before native reply."""
        if decision not in {"allow", "deny"}:
            raise StateError("Claude permission decision is invalid")
        parsed = ProtectedPayload.parse(payload)
        with self.state._immediate_transaction():
            self._current(launch)
            changed = self.connection.execute(
                "UPDATE claude_permission_requests SET status=?,consumed_at=? "
                "WHERE request_nonce=? AND launch_epoch=? AND payload_digest=? "
                "AND status='pending' AND expires_at>?",
                (
                    decision,
                    datetime.now(timezone.utc).isoformat(),
                    parsed.request_nonce,
                    launch.epoch,
                    _digest(payload),
                    int(time.time() * 1000),
                ),
            )
            if changed.rowcount != 1:
                raise StateError("Claude permission receipt is stale or already consumed")

    def revoke_request(self, launch: PermissionLaunch, nonce: str) -> None:
        """Close a failed wait without changing any already consumed decision."""
        canonical_uuid(nonce)
        with self.state._immediate_transaction():
            self.connection.execute(
                "UPDATE claude_permission_requests SET status='revoked' "
                "WHERE request_nonce=? AND launch_epoch=? AND status='pending'",
                (nonce, launch.epoch),
            )

    def close_launch(self, launch: PermissionLaunch) -> None:
        with self.state._immediate_transaction():
            self.connection.execute(
                "UPDATE claude_permission_launches SET status='closed' WHERE launch_epoch=?",
                (launch.epoch,),
            )
            self.connection.execute(
                "UPDATE claude_permission_requests SET status='revoked' "
                "WHERE launch_epoch=? AND status='pending'",
                (launch.epoch,),
            )
