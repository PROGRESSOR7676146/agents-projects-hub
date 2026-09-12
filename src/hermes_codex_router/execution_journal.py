from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .codex_failure import MAX_PARTIAL_TEXT
from .progress_delivery import ProgressDeliveryQueue
from .state import HubState, ProviderJobRecord, StateError


def _bounded(value: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StateError("invalid execution checkpoint value")
    return value


class ExecutionJournal:
    """Private task data, separate from diagnostic runtime events and admission snapshots."""

    def __init__(self, state: HubState, *, progress_enabled: bool = False) -> None:
        self.state = state
        self.connection = state._connection
        self.progress = ProgressDeliveryQueue(state) if progress_enabled else None

    def _lease(self, job_id: str, token: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM provider_jobs WHERE job_id = ? AND status = 'executing' "
            "AND lease_token = ? AND lease_expires_at > ?",
            (job_id, token, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if row is None:
            raise StateError("execution checkpoint requires a current invocation lease")
        return row

    def read(self, job_id: str) -> dict[str, str | None] | None:
        row = self.connection.execute(
            "SELECT * FROM provider_execution_checkpoints WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def record_thread(self, job_id: str, token: str, thread_id: str, cwd: Path) -> None:
        thread_id = _bounded(thread_id, 256)
        root = _bounded(str(cwd.resolve(strict=True)), 4096)
        with self.state._immediate_transaction():
            job = self._lease(job_id, token)
            prior = self.read(job_id)
            if prior and (
                prior["provider_thread_id"] != thread_id or prior["project_root"] != root
            ):
                raise StateError("execution thread binding is immutable")
            self.connection.execute(
                "INSERT OR IGNORE INTO provider_execution_checkpoints "
                "(job_id, provider_thread_id, project_root, updated_at) VALUES (?, ?, ?, ?)",
                (job_id, thread_id, root, datetime.now(timezone.utc).isoformat()),
            )
            changed = self.connection.execute(
                "UPDATE agent_sessions SET provider_session_id = ?, updated_at = ? "
                "WHERE session_id = ? AND generation = ? AND writer_mode = 'telegram'",
                (
                    thread_id,
                    datetime.now(timezone.utc).isoformat(),
                    job["session_id"],
                    job["session_generation"],
                ),
            )
            if changed.rowcount != 1:
                raise StateError("execution session generation or writer changed")

    def record_turn(self, job_id: str, token: str, turn_id: str) -> None:
        turn_id = _bounded(turn_id, 256)
        with self.state._immediate_transaction():
            self._lease(job_id, token)
            checkpoint = self.read(job_id)
            if checkpoint is None or checkpoint["provider_turn_id"] not in (None, turn_id):
                raise StateError("execution turn binding is missing or immutable")
            self.connection.execute(
                "UPDATE provider_execution_checkpoints SET provider_turn_id = ?, updated_at = ? "
                "WHERE job_id = ?",
                (turn_id, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def record_item(self, job_id: str, token: str, item_id: str, text: str, phase: str) -> None:
        item_id = _bounded(item_id, 256)
        text = _bounded(text, 200_000)
        if phase not in {"commentary", "final_answer", "unknown"}:
            raise StateError("only visible assistant phases may be checkpointed")
        with self.state._immediate_transaction():
            self._lease(job_id, token)
            checkpoint = self.read(job_id)
            if checkpoint is None or not checkpoint["provider_turn_id"]:
                raise StateError("visible item has no accepted turn binding")
            prior = self.connection.execute(
                "SELECT visible_text, phase FROM provider_visible_items WHERE job_id = ? AND item_id = ?",
                (job_id, item_id),
            ).fetchone()
            if prior:
                if tuple(prior) != (text, phase):
                    raise StateError("completed visible item changed")
                return
            count, size = self.connection.execute(
                "SELECT count(*), coalesce(sum(length(visible_text)), 0) "
                "FROM provider_visible_items WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if count >= 512 or size + len(text) > 200_000:
                raise StateError("visible execution journal limit reached")
            cursor = self.connection.execute(
                "INSERT INTO provider_visible_items (job_id, item_id, phase, visible_text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, item_id, phase, text, datetime.now(timezone.utc).isoformat()),
            )
            item_sequence = cursor.lastrowid
            if item_sequence is None:
                raise StateError("visible execution journal sequence is missing")
            if self.progress is not None and phase == "commentary":
                self.progress.enqueue_in_transaction(job_id, token, item_sequence, text)

    def record_completion(self, job_id: str, token: str, text: str) -> None:
        if not isinstance(text, str) or len(text) > 200_000:
            raise StateError("invalid completed checkpoint text")
        with self.state._immediate_transaction():
            self._lease(job_id, token)
            prior = self.read(job_id)
            if prior is None or not prior["provider_turn_id"]:
                raise StateError("completion has no accepted turn binding")
            if prior["completed_text"] is not None and prior["completed_text"] != text:
                raise StateError("completed checkpoint changed")
            self.connection.execute(
                "UPDATE provider_execution_checkpoints SET completed_text = ?, updated_at = ? "
                "WHERE job_id = ?",
                (text, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def partial_text(self, job_id: str) -> str:
        checkpoint = self.read(job_id)
        if checkpoint and checkpoint["completed_text"] is not None:
            text = checkpoint["completed_text"] or ""
        else:
            rows = self.connection.execute(
                "SELECT visible_text FROM provider_visible_items WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
            text = "\n\n".join(str(row[0]) for row in rows)
        if len(text) > MAX_PARTIAL_TEXT:
            return "[Earlier partial text omitted]\n" + text[-(MAX_PARTIAL_TEXT - 40) :]
        return text

    def claim_stale(self, agent_id: str, worker_id: str) -> ProviderJobRecord | None:
        now = datetime.now(timezone.utc)
        with self.state._immediate_transaction():
            row = self.connection.execute(
                "SELECT job_id FROM provider_jobs WHERE agent_id = ? AND status = 'executing' "
                "AND lease_expires_at <= ? ORDER BY created_at LIMIT 1",
                (agent_id, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            # A recovery lease authorizes only reading/committing an existing outcome.
            # It never changes status to queued or increments invocation attempts.
            self.connection.execute(
                "UPDATE provider_jobs SET lease_token = ?, lease_owner = ?, lease_expires_at = ? "
                "WHERE job_id = ?",
                (str(uuid.uuid4()), worker_id, (now + timedelta(seconds=120)).isoformat(), row[0]),
            )
            return self.state.get_provider_job(str(row[0]))
