"""Populated schema44 notice rebuild retains all receipts, links and rollback."""

from __future__ import annotations

import re
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from hermes_codex_router import migrations
from tests import test_task_notice_migration as fixtures
from tests.test_delivery_certainty_migration import snapshot


def canonical_sql(sql: str | None) -> str | None:
    if sql is None:
        return None
    sql = re.sub(r'"(task_lifecycle_notices|task_lifecycle_legacy_stop_links)"', r"\1", sql)
    return re.sub(r"\s*([(),])\s*", r"\1", " ".join(sql.split()))


def incoming_notice_fks(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    incoming = {}
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        quoted = table.replace('"', '""')
        references = [
            tuple(row)
            for row in connection.execute(f'PRAGMA foreign_key_list("{quoted}")')
            if row[2] == "task_lifecycle_notices"
        ]
        if references:
            incoming[table] = references
    return incoming


class OutcomeAssessmentMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TaskNoticeMigrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.path = self.fixture.path
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 44):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as old, old:
            for number, status in enumerate(
                ("pending", "leased", "delivered", "unknown", "failed", "superseded"), 1
            ):
                old.execute(
                    """INSERT INTO task_lifecycle_notices
                       (notice_id,event_key,kind,job_id,chat_id,thread_id,reply_to_message_id,
                        telegram_html,status,attempt_count,available_at,lease_token,lease_owner,
                        lease_expires_at,send_started_at,telegram_message_id,error_code,created_at,updated_at)
                       VALUES (?,?,'example', 'job-provider',-1001234567890,80,?,
                               'Example notice',?,2,'2026-01-01',?,?,?,? ,?,'example',
                               '2026-01-01','2026-01-02')""",
                    (
                        f"example-{status}",
                        f"example-event-{status}",
                        number,
                        status,
                        "example-token" if status == "leased" else None,
                        "example-sender" if status == "leased" else None,
                        "2099-01-01" if status == "leased" else None,
                        "2026-01-02" if status in {"unknown", "delivered"} else None,
                        500 + number if status == "delivered" else None,
                    ),
                )
            self.assertTrue(
                old.execute("SELECT * FROM task_lifecycle_legacy_stop_links").fetchall()
            )
            self.before = snapshot(old)
            self.objects = old.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            self.notice_objects = {
                name: (kind, canonical_sql(sql))
                for kind, name, sql in old.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN (?,?)",
                    ("task_lifecycle_notices", "task_lifecycle_legacy_stop_links"),
                )
            }
            self.incoming_fks = incoming_notice_fks(old)
            self.assertEqual(set(self.incoming_fks), {"task_lifecycle_legacy_stop_links"})
            self.notice_columns = old.execute(
                "PRAGMA table_info(task_lifecycle_notices)"
            ).fetchall()
            self.notice_fks = {
                tuple(row[2:])
                for row in old.execute("PRAGMA foreign_key_list(task_lifecycle_notices)")
            }

    def test_populated_upgrade_preserves_all_notice_states_links_and_private_backup(self) -> None:
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 45):
            result = migrations.migrate_database(self.path)
        self.assertEqual((result.previous_version, result.current_version), (44, 45))
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(snapshot(backup), self.before)
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 44)
        with closing(sqlite3.connect(self.path)) as upgraded:
            after = snapshot(upgraded)
            self.assertEqual(set(after) - set(self.before), {"outcome_assessment_dispositions"})
            for table, rows in self.before.items():
                expected = (
                    [(*row, None) for row in rows] if table == "task_lifecycle_notices" else rows
                )
                self.assertEqual(after[table], expected, table)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            indexes = {
                row[0]
                for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            self.assertTrue(
                {"task_notice_due", "task_notice_stop", "task_notice_assessment"} <= indexes
            )
            self.assertEqual(
                upgraded.execute(
                    "PRAGMA foreign_key_list(task_lifecycle_legacy_stop_links)"
                ).fetchone()[2],
                "task_lifecycle_notices",
            )
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 45):
            self.assertIsNone(migrations.migrate_database(self.path).backup_path)

    def test_rebuild_fault_rolls_back_rows_objects_and_schema_in_place(self) -> None:
        with (
            patch.object(
                migrations, "MIGRATION_45", migrations.MIGRATION_45 + "\nINVALID EXAMPLE SQL;"
            ),
            self.assertRaises(sqlite3.Error),
        ):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as retained:
            self.assertEqual(snapshot(retained), self.before)
            self.assertEqual(retained.execute("PRAGMA user_version").fetchone()[0], 44)
            self.assertEqual(
                retained.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY name"
                ).fetchall(),
                self.objects,
            )
            self.assertEqual(retained.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_notice_rebuild_preserves_exact_existing_objects_constraints_and_incoming_fks(
        self,
    ) -> None:
        with patch.object(migrations, "LATEST_SCHEMA_VERSION", 45):
            migrations.migrate_database(self.path, create_backup=False)
        with closing(sqlite3.connect(self.path)) as upgraded:
            after_objects = {
                name: (kind, canonical_sql(sql))
                for kind, name, sql in upgraded.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN (?,?)",
                    ("task_lifecycle_notices", "task_lifecycle_legacy_stop_links"),
                )
            }
            self.assertEqual(
                set(after_objects), set(self.notice_objects) | {"task_notice_assessment"}
            )
            for name, (kind, sql) in self.notice_objects.items():
                after_kind, after_sql = after_objects[name]
                if name == "task_lifecycle_notices":
                    assert after_sql is not None
                    after_sql = after_sql.replace(
                        "assessment_disposition_id TEXT REFERENCES outcome_assessment_dispositions(disposition_id),",
                        "",
                    ).replace(" OR assessment_disposition_id IS NOT NULL", "")
                self.assertEqual((after_kind, after_sql), (kind, sql), name)
            columns = upgraded.execute("PRAGMA table_info(task_lifecycle_notices)").fetchall()
            self.assertEqual(columns[:-1], self.notice_columns)
            self.assertEqual(columns[-1][1:], ("assessment_disposition_id", "TEXT", 0, None, 0))
            self.assertEqual(
                {
                    tuple(row[2:])
                    for row in upgraded.execute("PRAGMA foreign_key_list(task_lifecycle_notices)")
                },
                self.notice_fks
                | {
                    (
                        "outcome_assessment_dispositions",
                        "assessment_disposition_id",
                        "disposition_id",
                        "NO ACTION",
                        "NO ACTION",
                        "NONE",
                    )
                },
            )
            self.assertEqual(incoming_notice_fks(upgraded), self.incoming_fks)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
