"""Append one Telegram part to a still-collecting queued batch (REQ-ROUTE-009).

Every method runs inside a HubState-owned immediate transaction and never
commits or opens one. A part joins only an unleased tail job whose quiet and
absolute windows are both open; otherwise the caller keeps its own fallback.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Sequence

from .incoming_materials import IncomingMaterialDraft
from .root_blockers import persistent_root_blocker
from .state_errors import StateError
from .state_values import _timestamp
from .telegram_quoted_context import FORWARDED_PART_SEPARATOR

if TYPE_CHECKING:
    from .state import HubState

BATCH_PAYLOAD_LIMIT = 20000


class BatchPartState:
    def __init__(self, state: HubState) -> None:
        self.state = state

    @staticmethod
    def context_covered(candidate: sqlite3.Row, context_watermark: int | None) -> bool:
        """Whether the candidate already carries all passive context of a new part."""
        if context_watermark is None:
            return True
        covered = candidate["context_watermark"]
        return covered is not None and context_watermark <= int(covered)

    def append_in_transaction(
        self,
        candidate: sqlite3.Row,
        *,
        chat_id: int,
        message_id: int,
        part_text: str,
        separator: str,
        materials: Sequence[IncomingMaterialDraft],
        current: datetime,
        quiet_ms: int,
        max_ms: int,
        origin: str | None = None,
    ) -> bool:
        """Join the candidate, or return False when the payload would overflow."""
        combined = str(candidate["payload_text"]) + separator + part_text
        if len(combined) > BATCH_PAYLOAD_LIMIT:
            return False
        connection = self.state._connection
        job_id = str(candidate["job_id"])
        timestamp = _timestamp(current)
        part = (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_job_inputs WHERE job_id = ?", (job_id,)
                ).fetchone()[0]
            )
            + 1
        )
        hard_deadline = datetime.fromisoformat(str(candidate["created_at"])) + timedelta(
            milliseconds=max_ms
        )
        next_attempt = min(current + timedelta(milliseconds=quiet_ms), hard_deadline)
        connection.execute(
            """INSERT INTO observed_messages
               (chat_id, message_id, observer_agent_id, observed_at)
               VALUES (?, ?, 'hub', ?)""",
            (chat_id, message_id, timestamp),
        )
        connection.execute(
            """INSERT INTO provider_job_inputs
               (job_id, chat_id, message_id, part_index, input_text, received_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, chat_id, message_id, part, part_text, timestamp),
        )
        scope = {
            "topic_id": int(candidate["topic_id"]),
            "agent_id": str(candidate["agent_id"]),
            "session_id": str(candidate["session_id"]),
            "session_generation": int(candidate["session_generation"]),
        }
        self.state._attach_forwarded_materials(job_id=job_id, **scope)
        self.state._insert_incoming_materials(
            job_id=job_id,
            chat_id=chat_id,
            message_id=message_id,
            materials=materials,
            timestamp=timestamp,
            origin=origin,
            **scope,
        )
        connection.execute(
            """UPDATE provider_jobs
               SET payload_text = ?, next_attempt_at = ?, updated_at = ?
               WHERE job_id = ? AND status = 'queued'""",
            (combined, _timestamp(next_attempt), timestamp, job_id),
        )
        return True

    def join_forward_in_transaction(
        self,
        *,
        topic_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        materials: Sequence[IncomingMaterialDraft],
        quiet_ms: int,
        max_ms: int,
        telegram_ingress_identity: str | None,
    ) -> bool:
        """Let a forward reach the turn its burst is still collecting (REQ-CTX-006)."""
        current = datetime.now(timezone.utc)
        candidate = self._forward_candidate(
            topic_id=topic_id,
            message_id=message_id,
            current=current,
            max_ms=max_ms,
            telegram_ingress_identity=telegram_ingress_identity,
        )
        return candidate is not None and self.append_in_transaction(
            candidate,
            chat_id=chat_id,
            message_id=message_id,
            part_text=text,
            separator=FORWARDED_PART_SEPARATOR,
            materials=materials,
            current=current,
            quiet_ms=quiet_ms,
            max_ms=max_ms,
            origin="forward",
        )

    def _forward_candidate(
        self,
        *,
        topic_id: int,
        message_id: int,
        current: datetime,
        max_ms: int,
        telegram_ingress_identity: str | None,
    ) -> sqlite3.Row | None:
        """Find the topic's collecting tail turn that a passive forward may join.

        A forward is never addressed, so it joins only the turn the user has just
        started in the same burst, under its unchanged session generation.
        """
        from .session_adoption_state import CodexSessionOrigins

        connection = self.state._connection
        candidate = connection.execute(
            """SELECT jobs.* FROM provider_jobs jobs
               JOIN agent_sessions sessions ON sessions.session_id = jobs.session_id
               WHERE jobs.topic_id = ? AND jobs.status = 'queued'
                 AND jobs.next_attempt_at > ? AND jobs.created_at >= ?
                 AND jobs.input_group_key IS NULL
                 AND (SELECT ingress_identity FROM provider_job_telegram_ingress ingress
                      WHERE ingress.job_id = jobs.job_id) IS ?
                 AND sessions.topic_id = jobs.topic_id AND sessions.agent_id = jobs.agent_id
                 AND sessions.generation = jobs.session_generation
                 AND sessions.status IN ('active', 'satellite')
                 AND sessions.writer_mode = 'telegram'
                 AND sessions.model = jobs.model AND sessions.effort = jobs.effort
                 AND jobs.codex_permission_profile IS sessions.codex_permission_profile
                 AND jobs.topic_sequence = (
                     SELECT MAX(tail.topic_sequence) FROM provider_jobs tail
                     WHERE tail.topic_id = jobs.topic_id
                 )""",
            (
                topic_id,
                _timestamp(current),
                _timestamp(current - timedelta(milliseconds=max_ms)),
                telegram_ingress_identity,
            ),
        ).fetchone()
        if candidate is None or persistent_root_blocker(connection, topic_id=topic_id) is not None:
            return None
        session_id = str(candidate["session_id"])
        try:
            # The same selection guard as an ordinary follow-up: a changed Codex
            # permission selection keeps the forward passive.
            self.state._sessions_state.require_codex_selection(self.state.get_session(session_id))
            CodexSessionOrigins(self.state).require_admission(session_id, message_id)
        except StateError:  # includes CodexPermissionSelectionChanged
            return None
        return candidate
