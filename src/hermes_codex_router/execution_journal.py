from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .codex_failure import MAX_PARTIAL_TEXT
from .progress_delivery import ProgressDeliveryQueue
from .state import HubState, ProviderJobRecord, SessionRecord, StateError

CLAUDE_PRE_INVOCATION_ERROR_CODES = frozenset(
    {
        "claude_cpa_route_unverified",
        "claude_cpa_credential_ambiguous",
        "claude_cli_unavailable",
        "claude_cli_capabilities_unverified",
    }
)


def _bounded(value: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StateError("invalid execution checkpoint value")
    return value


@dataclass(frozen=True, slots=True)
class ClaudeSessionBinding:
    session_id: str
    is_new: bool


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

    def prepare_claude_session(self, job_id: str, token: str, cwd: Path) -> ClaudeSessionBinding:
        """Commit a native UUID before invocation without manufacturing turn evidence."""
        root = _bounded(str(cwd.resolve(strict=True)), 4096)
        if not Path(root).is_dir():
            raise StateError("Claude execution root is not a directory")
        with self.state._immediate_transaction():
            job = self._lease(job_id, token)
            session = self._claude_owner(job, root)
            if self.read(job_id) is not None:
                raise StateError("Claude invocation was already prepared")
            current = session.provider_session_id
            is_new = current is None
            if current is not None:
                self._claude_uuid(current)
                roots = self.connection.execute(
                    "SELECT checkpoint.project_root FROM provider_execution_checkpoints checkpoint "
                    "JOIN provider_jobs prior ON prior.job_id=checkpoint.job_id "
                    "WHERE prior.session_id=? AND prior.session_generation=? "
                    "AND prior.topic_id=? AND prior.agent_id=? "
                    "AND checkpoint.provider_thread_id=? AND prior.job_id != ?",
                    (
                        session.session_id,
                        session.generation,
                        session.topic_id,
                        session.agent_id,
                        current,
                        job_id,
                    ),
                ).fetchall()
                if not roots or any(row[0] != root for row in roots):
                    raise StateError("Claude session root provenance is missing or conflicting")
                prior = self.connection.execute(
                    "SELECT prior.status,prior.error_class,prior.error_code,checkpoint.job_id,"
                    "checkpoint.provider_turn_id,checkpoint.completed_text,"
                    "EXISTS(SELECT 1 FROM provider_visible_items items "
                    "WHERE items.job_id=prior.job_id) AS has_visible "
                    "FROM provider_jobs prior LEFT JOIN provider_execution_checkpoints checkpoint "
                    "ON checkpoint.job_id=prior.job_id "
                    "WHERE prior.session_id=? AND prior.session_generation=? "
                    "AND prior.topic_id=? AND prior.agent_id=? "
                    "AND prior.job_id != ? AND (checkpoint.provider_thread_id=? OR "
                    "(checkpoint.job_id IS NULL AND prior.provider_started_at IS NOT NULL "
                    "AND (prior.provider_session_id IS NULL OR prior.provider_session_id=?)))",
                    (
                        session.session_id,
                        session.generation,
                        session.topic_id,
                        session.agent_id,
                        job_id,
                        current,
                        current,
                    ),
                ).fetchall()
                # Missing historical checkpoints cannot prove that an allocated
                # UUID was never invoked, even if later attempts failed to launch.
                is_new = bool(prior) and all(
                    row["job_id"] is not None
                    and row["status"] == "failed"
                    and row["error_class"] == "pre_execution"
                    and row["error_code"] in CLAUDE_PRE_INVOCATION_ERROR_CODES
                    and row["provider_turn_id"] is None
                    and row["completed_text"] is None
                    and not row["has_visible"]
                    for row in prior
                )
            identifier = current if current is not None else str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                "INSERT INTO provider_execution_checkpoints "
                "(job_id,provider_thread_id,project_root,updated_at) VALUES (?,?,?,?)",
                (job_id, identifier, root, now),
            )
            self.connection.execute(
                "UPDATE agent_sessions SET provider_session_id=?,updated_at=? WHERE session_id=?",
                (identifier, now, session.session_id),
            )
            return ClaudeSessionBinding(identifier, is_new)

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
            item_sequence = self._insert_visible_item(job_id, item_id, text, phase)
            if self.progress is not None and phase == "commentary" and item_sequence is not None:
                self.progress.enqueue_in_transaction(job_id, token, item_sequence, text)

    def _insert_visible_item(self, job_id: str, item_id: str, text: str, phase: str) -> int | None:
        """Insert under the caller's transaction; exact duplicates have no new sequence."""
        prior = self.connection.execute(
            "SELECT visible_text, phase FROM provider_visible_items WHERE job_id = ? AND item_id = ?",
            (job_id, item_id),
        ).fetchone()
        if prior:
            if tuple(prior) != (text, phase):
                raise StateError("completed visible item changed")
            return
        # SQLite length(TEXT) stops at NUL. Count the full Unicode strings so
        # embedded NUL cannot weaken the shared Codex/Claude journal budget.
        rows = self.connection.execute(
            "SELECT visible_text FROM provider_visible_items WHERE job_id = ?", (job_id,)
        ).fetchall()
        count, size = len(rows), sum(len(row[0]) for row in rows)
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
        return item_sequence

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

    @staticmethod
    def _claude_uuid(value: str) -> str:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except (ValueError, AttributeError, TypeError):
            pass
        raise StateError("Claude identity is not a canonical UUID")

    def _claude_owner(self, job: sqlite3.Row, root: str) -> SessionRecord:
        session = self.state.get_session(str(job["session_id"]))
        topic = self.state.get_topic(int(job["topic_id"]))
        if (
            session.topic_id != job["topic_id"]
            or session.agent_id != job["agent_id"]
            or session.generation != job["session_generation"]
            or session.writer_mode != "telegram"
            or session.status == "archived"
            or job["provider_session_id"] not in (None, session.provider_session_id)
            or topic.chat_id != job["chat_id"]
            or topic.execution_scope not in (f"root:{root}", f"project:{topic.project_id}")
        ):
            raise StateError("Claude session ownership or root changed")
        return session

    def _claude_checkpoint(
        self, job_id: str, token: str, session_id: str, cwd: Path
    ) -> dict[str, str | None]:
        """Validate the trusted worker binding inside its journal transaction."""
        session_id = self._claude_uuid(session_id)
        root = str(cwd.resolve(strict=True))
        if not Path(root).is_dir():
            raise StateError("Claude execution root is not a directory")
        job = self._lease(job_id, token)
        session = self._claude_owner(job, root)
        checkpoint = self.read(job_id)
        if (
            session.provider_session_id != session_id
            or checkpoint is None
            or checkpoint["provider_thread_id"] != session_id
            or checkpoint["provider_turn_id"] is not None
            or checkpoint["project_root"] != root
        ):
            raise StateError("Claude invocation binding changed or is missing")
        return checkpoint

    def record_claude_item(
        self, job_id: str, token: str, session_id: str, message_id: str, text: str, *, cwd: Path
    ) -> None:
        message_id = self._claude_uuid(message_id)
        text = _bounded(text, 200_000)
        with self.state._immediate_transaction():
            checkpoint = self._claude_checkpoint(job_id, token, session_id, cwd)
            if checkpoint["completed_text"] is not None:
                raise StateError("Claude invocation is already complete")
            self._insert_visible_item(job_id, message_id, text, "unknown")

    def record_claude_completion(
        self, job_id: str, token: str, session_id: str, text: str, *, cwd: Path
    ) -> None:
        if not isinstance(text, str) or len(text) > 200_000:
            raise StateError("invalid completed checkpoint text")
        with self.state._immediate_transaction():
            checkpoint = self._claude_checkpoint(job_id, token, session_id, cwd)
            if checkpoint["completed_text"] is not None:
                if checkpoint["completed_text"] != text:
                    raise StateError("completed checkpoint changed")
                return
            self.connection.execute(
                "UPDATE provider_execution_checkpoints SET completed_text=?,updated_at=? "
                "WHERE job_id=?",
                (text, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def validated_claude_partial(
        self, job_id: str, token: str, session_id: str, *, cwd: Path
    ) -> str:
        with self.state._immediate_transaction():
            self._claude_checkpoint(job_id, token, session_id, cwd)
            return self.partial_text(job_id)

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
