"""Separate Hub material notice from raw native Claude completion."""

from __future__ import annotations

import sqlite3

MAX_CLAUDE_MATERIAL_NOTICE_CHARACTERS = 8192


def ensure_claude_material_notice_column(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(provider_execution_checkpoints)")
    }
    if "claude_material_notice" not in columns:
        connection.execute(
            """ALTER TABLE provider_execution_checkpoints ADD COLUMN claude_material_notice TEXT
            CHECK(claude_material_notice IS NULL OR (
                completed_text IS NOT NULL AND length(claude_material_notice) <= 8192
                AND instr(claude_material_notice, char(0)) = 0
            ))"""
        )


CLAUDE_MATERIAL_NOTICE_SCHEMA = """
CREATE TRIGGER IF NOT EXISTS claude_completed_material_notice_immutable
BEFORE UPDATE OF claude_material_notice ON provider_execution_checkpoints
WHEN OLD.completed_text IS NOT NULL AND NEW.claude_material_notice IS NOT OLD.claude_material_notice
BEGIN SELECT RAISE(ABORT, 'completed Claude material notice is immutable'); END;
"""
