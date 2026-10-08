"""Dependency-neutral exclusion of an unquiesced exact-turn control sender."""

from __future__ import annotations

import sqlite3

from .state_errors import ControlScopeError, StateError


def control_owner_for_topic(connection: sqlite3.Connection, topic_id: int) -> sqlite3.Row | None:
    """Only the empty global owner set waives exact scope resolution.

    The caller keeps this read and the authority change in the same transaction.
    An unresolved alias cannot infer absence of an owner by project identity.
    """
    any_owner = connection.execute(
        """SELECT 1 FROM codex_turn_controls
           WHERE send_started_at IS NOT NULL AND owner_quiesced_at IS NULL LIMIT 1"""
    ).fetchone()
    if any_owner is None:
        return None
    return unquiesced_control_owner(connection, resolved_control_scope(connection, topic_id))


def require_control_scope_clear(connection: sqlite3.Connection, execution_scope: str) -> None:
    """Exact validated root gate; no permissive project-identity fallback."""
    if not execution_scope.startswith("root:/"):
        raise ControlScopeError("unresolved")
    if unquiesced_control_owner(connection, execution_scope) is not None:
        raise StateError("native control operation has not confirmed quiescence")


def resolved_control_scope(connection: sqlite3.Connection, topic_id: int) -> str:
    """Use exact durable roots for legacy bindings; never re-resolve host paths."""
    topic = connection.execute(
        "SELECT project_id,execution_scope FROM topics WHERE topic_id=?", (topic_id,)
    ).fetchone()
    if topic is None:
        raise StateError("unknown control topic")
    scope = topic["execution_scope"]
    if isinstance(scope, str) and scope.startswith("root:/"):
        return scope
    if scope not in (None, f"project:{topic['project_id']}"):
        raise ControlScopeError("unresolved")
    roots = {
        str(row["project_root"])
        for row in connection.execute(
            """SELECT project_root FROM codex_turn_controls WHERE topic_id=?
               UNION SELECT checkpoint.project_root FROM provider_execution_checkpoints checkpoint
               JOIN provider_jobs job ON job.job_id=checkpoint.job_id WHERE job.topic_id=?
               UNION SELECT origin.canonical_root AS project_root FROM codex_session_origins origin
               JOIN agent_sessions session ON session.session_id=origin.session_id WHERE session.topic_id=?""",
            (topic_id, topic_id, topic_id),
        ).fetchall()
        if row["project_root"] is not None
    }
    if len(roots) > 1:
        raise ControlScopeError("ambiguous")
    if not roots or not next(iter(roots)).startswith("/"):
        raise ControlScopeError("unresolved")
    return "root:" + roots.pop()


def control_blocks_scope(scope_expression: str) -> str:
    """Only trusted internal SQL expressions; target root ignores mutable topic scope."""
    return f"""EXISTS (
        SELECT 1 FROM codex_turn_controls control_owner
        WHERE control_owner.send_started_at IS NOT NULL
          AND control_owner.owner_quiesced_at IS NULL
          AND control_owner.project_root = substr({scope_expression},6)
    )"""


def control_scope_is_clear(scope_expression: str, *, topic_id_expression: str) -> str:
    """SQL eligibility: an unknown alias cannot bypass a nonempty owner set."""
    # Match the Python resolver without mutating a retained topic binding.
    resolved = f"""CASE WHEN substr({scope_expression},1,6)='root:/' THEN {scope_expression}
        WHEN {scope_expression} IS NULL OR {scope_expression} =
             'project:' || (SELECT project_id FROM topics WHERE topic_id={topic_id_expression})
        THEN (SELECT CASE WHEN COUNT(*)=1 AND MIN(project_root) LIKE '/%'
                          THEN 'root:' || MIN(project_root) END FROM (
            SELECT project_root FROM codex_turn_controls WHERE topic_id={topic_id_expression}
            UNION SELECT checkpoint.project_root FROM provider_execution_checkpoints checkpoint
                  JOIN provider_jobs saved_job ON saved_job.job_id=checkpoint.job_id
                  WHERE saved_job.topic_id={topic_id_expression}
            UNION SELECT origin.canonical_root FROM codex_session_origins origin
                  JOIN agent_sessions saved_session ON saved_session.session_id=origin.session_id
                  WHERE saved_session.topic_id={topic_id_expression}
        )) END"""
    return f"""(
        NOT EXISTS (SELECT 1 FROM codex_turn_controls global_control
                    WHERE global_control.send_started_at IS NOT NULL
                      AND global_control.owner_quiesced_at IS NULL)
        OR (substr(({resolved}),1,6)='root:/' AND NOT {control_blocks_scope(resolved)})
    )"""


def unquiesced_control_owner(
    connection: sqlite3.Connection, execution_scope: str
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM codex_turn_controls
           WHERE send_started_at IS NOT NULL AND owner_quiesced_at IS NULL
             AND project_root=? ORDER BY send_started_at,job_id LIMIT 1""",
        (execution_scope.removeprefix("root:"),),
    ).fetchone()
