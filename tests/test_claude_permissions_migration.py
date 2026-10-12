from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import HubState
from tests.schema_fixtures import project_historical_database


def create_version37(path: Path) -> dict[str, list[tuple[object, ...]]]:
    source = path.with_name("example-current-seed.db")
    with patch.object(migrations, "LATEST_SCHEMA_VERSION", 48):
        state = HubState.open(source, codex_permission_profile=None)
        try:
            topic = state.observe_topic(
                project_id="example-project", chat_id=-1001234567890, thread_id=7, title="Example"
            )
            session = state.activate_agent(topic.topic_id, "claude", "example-model", "high")
            state.enqueue_provider_job(
                idempotency_key="example:1",
                chat_id=topic.chat_id,
                message_id=1,
                topic_id=topic.topic_id,
                agent_id="claude",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="Example queued work",
            )
        finally:
            state.close()
    project_historical_database(source, path, 37)
    return rows(path)


def rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as connection:
        names = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            name: connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
            for name in names
            if not name.startswith("claude_permission_")
        }


class ClaudePermissionsMigrationTests(unittest.TestCase):
    def test_additive_upgrade_preserves_all_rows_and_private_version37_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            expected = create_version37(path)
            with patch.object(migrations, "LATEST_SCHEMA_VERSION", 38):
                result = migrations.migrate_database(path)
            self.assertEqual((result.previous_version, result.current_version), (37, 38))
            self.assertEqual(rows(path), expected)
            assert result.backup_path is not None
            self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(rows(result.backup_path), expected)
            with sqlite3.connect(result.backup_path) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 37)
            with sqlite3.connect(path) as upgraded:
                self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    upgraded.execute("SELECT count(*) FROM claude_permission_requests").fetchone()[
                        0
                    ],
                    0,
                )

    def test_failure_after_first_ddl_rolls_back_version_and_partial_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            expected = create_version37(path)
            first_statement = migrations.MIGRATION_38.split(";", 1)[0] + ";"
            failing_script = first_statement + "\nSELECT * FROM absent_example_table;"
            with patch.object(migrations, "MIGRATION_38", failing_script):
                with self.assertRaises(sqlite3.OperationalError):
                    migrations.migrate_database(path, create_backup=False)
            self.assertEqual(rows(path), expected)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 37)
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name LIKE 'claude_permission_%'"
                    ).fetchall(),
                    [],
                )
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
