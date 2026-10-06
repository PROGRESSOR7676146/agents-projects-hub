"""Additive selection snapshots; no fabricated effective-policy attestation."""

from __future__ import annotations

import sqlite3

PROFILE_TABLES = (
    "agent_sessions",
    "provider_jobs",
    "provider_execution_checkpoints",
    "session_connect_workflows",
)


def ensure_codex_permission_columns(connection: sqlite3.Connection) -> None:
    for table in PROFILE_TABLES:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "codex_permission_profile" not in columns:
            connection.execute(
                f"""ALTER TABLE {table} ADD COLUMN codex_permission_profile TEXT
                    CHECK(codex_permission_profile IS NULL OR (
                        length(codex_permission_profile) BETWEEN 1 AND 64
                        AND codex_permission_profile GLOB '[a-z]*'
                        AND codex_permission_profile NOT GLOB '*[^a-z0-9_-]*'
                    ))"""
            )


CODEX_PERMISSIONS_SCHEMA = "\n".join(
    f"""CREATE TRIGGER IF NOT EXISTS {table}_codex_profile_immutable
        BEFORE UPDATE OF codex_permission_profile ON {table}
        WHEN NEW.codex_permission_profile IS NOT OLD.codex_permission_profile
        BEGIN SELECT RAISE(ABORT, 'Codex permission selection is immutable'); END;"""
    for table in PROFILE_TABLES
)

CODEX_PERMISSIONS_SCHEMA += """
CREATE TRIGGER IF NOT EXISTS provider_jobs_codex_profile_snapshot
BEFORE INSERT ON provider_jobs
WHEN NEW.agent_id = 'codex' AND NEW.codex_permission_profile IS NOT
     (SELECT codex_permission_profile FROM agent_sessions WHERE session_id=NEW.session_id)
BEGIN SELECT RAISE(ABORT, 'Codex job permission snapshot mismatch'); END;
CREATE TRIGGER IF NOT EXISTS provider_execution_checkpoints_codex_profile_snapshot
BEFORE INSERT ON provider_execution_checkpoints
WHEN NEW.codex_permission_profile IS NOT
     (SELECT codex_permission_profile FROM provider_jobs WHERE job_id=NEW.job_id)
BEGIN SELECT RAISE(ABORT, 'Codex checkpoint permission snapshot mismatch'); END;
"""
