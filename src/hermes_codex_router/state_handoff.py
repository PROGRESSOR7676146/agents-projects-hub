"""Model-selection provenance for a delivered session handoff."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state_sessions import SessionRecord


@dataclass(frozen=True, slots=True)
class HandoffAdmission:
    source_session_id: str
    source_sequence: int


def _prefix(topic_id: int) -> str:
    return f"handoff-model:{topic_id}:"


def clear_model_handoff(db: sqlite3.Connection, topic_id: int) -> None:
    db.execute(
        "DELETE FROM runtime_checkpoints WHERE checkpoint_key GLOB ?",
        (_prefix(topic_id) + "*",),
    )


def model_handoff_source_generation(db: sqlite3.Connection, target: SessionRecord) -> int | None:
    if target.agent_id != "codex" or target.provider_session_id is not None:
        return None
    row = db.execute(
        """SELECT checkpoint.integer_value FROM runtime_checkpoints AS checkpoint
           JOIN agent_sessions AS session ON session.session_id = ?
           WHERE checkpoint.checkpoint_key = ? AND session.updated_at = checkpoint.updated_at
             AND NOT EXISTS (
                 SELECT 1 FROM provider_jobs WHERE topic_id = ? AND session_id = ?
             )""",
        (
            target.session_id,
            _prefix(target.topic_id) + target.session_id,
            target.topic_id,
            target.session_id,
        ),
    ).fetchone()
    return None if row is None else int(row[0])


def remember_model_handoff(
    db: sqlite3.Connection, target: SessionRecord, source_generation: int
) -> None:
    """Keep one origin per topic, including across successive empty model changes."""
    clear_model_handoff(db, target.topic_id)
    # Older runtimes do not clear the checkpoint on provider switches, but do
    # update the session row. The original row timestamp makes that fail closed.
    db.execute(
        """INSERT INTO runtime_checkpoints(checkpoint_key, integer_value, updated_at)
           SELECT ?, ?, updated_at FROM agent_sessions WHERE session_id = ?""",
        (_prefix(target.topic_id) + target.session_id, source_generation, target.session_id),
    )


def is_model_handoff_successor(
    db: sqlite3.Connection, source: SessionRecord, target: SessionRecord
) -> bool:
    return (
        source.topic_id == target.topic_id
        and source.agent_id == "codex"
        and source.status == "archived"
        and target.status == "active"
        and model_handoff_source_generation(db, target) == source.generation
    )


def has_later_handoff_input(
    db: sqlite3.Connection, topic_id: int, handoff: HandoffAdmission
) -> bool:
    row = db.execute(
        """SELECT 1 FROM provider_jobs
           WHERE topic_id = ? AND session_id = ? AND topic_sequence > ? LIMIT 1""",
        (topic_id, handoff.source_session_id, handoff.source_sequence),
    ).fetchone()
    return row is not None
