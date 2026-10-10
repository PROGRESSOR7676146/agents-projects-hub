from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.state import StateError
from tests import test_claude_invocation_journal as fixtures


class ClaudeMaterialNoticeJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudeInvocationJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def complete(self, notice: str = "") -> None:
        f = self.fixture
        f.journal.record_claude_completion(
            f.job.job_id,
            f.token,
            f.binding.session_id,
            "Example raw completion",
            cwd=f.root,
            material_notice=notice,
        )

    def test_raw_completion_and_original_notice_commit_together_and_are_idempotent(self) -> None:
        notice = "\n\nIncoming material unavailable: example.gif is unsupported"
        self.complete(notice)
        with sqlite3.connect(self.fixture.path) as observer:
            self.assertEqual(
                observer.execute(
                    "SELECT completed_text,claude_material_notice FROM provider_execution_checkpoints"
                ).fetchone(),
                ("Example raw completion", notice),
            )
        before = self.fixture.snapshot()
        self.complete(notice)
        self.assertEqual(self.fixture.snapshot(), before)
        with self.assertRaises(StateError):
            self.complete("Changed disposition")
        self.assertEqual(self.fixture.snapshot(), before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.fixture.mutate(
                "UPDATE provider_execution_checkpoints SET claude_material_notice='changed'", ()
            )

    def test_invalid_notice_refuses_without_losing_completion_evidence(self) -> None:
        before = self.fixture.snapshot()
        for notice in ("x" * 8193, "example\x00notice", None, 1):
            with self.subTest(kind=type(notice).__name__), self.assertRaises(StateError):
                self.complete(notice)  # type: ignore[arg-type]
            self.assertEqual(self.fixture.snapshot(), before)
        self.complete("x" * 8192)

    def test_save_fault_rolls_back_both_completion_and_notice(self) -> None:
        self.fixture.mutate(
            "CREATE TRIGGER example_completion_fault BEFORE UPDATE OF completed_text ON provider_execution_checkpoints BEGIN SELECT RAISE(ABORT,'example fault'); END",
            (),
        )
        before = self.fixture.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete("Example original notice")
        self.assertEqual(self.fixture.snapshot(), before)

    def legacy(self) -> None:
        f = self.fixture
        with f.state._connection:
            f.state._connection.execute(
                "UPDATE provider_execution_checkpoints SET completed_text='Example legacy completion'"
            )
            f.state._connection.execute("DROP TRIGGER claude_completed_material_notice_immutable")
            f.state._connection.execute(
                "ALTER TABLE provider_execution_checkpoints DROP COLUMN claude_material_notice"
            )
            f.state._connection.execute("PRAGMA user_version=52")

    def test_migration_preserves_legacy_null_raw_completion_jobs_and_backup(self) -> None:
        self.legacy()
        f = self.fixture
        jobs = f.state._connection.execute("SELECT * FROM provider_jobs").fetchall()
        result = migrations.migrate_database(f.path)
        self.assertEqual((result.previous_version, result.current_version), (52, 53))
        self.assertIsNotNone(result.backup_path)
        assert result.backup_path is not None
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 52)
            self.assertNotIn(
                "claude_material_notice",
                [
                    row[1]
                    for row in backup.execute("PRAGMA table_info(provider_execution_checkpoints)")
                ],
            )
        self.assertEqual(
            f.state._connection.execute("SELECT * FROM provider_jobs").fetchall(), jobs
        )
        checkpoint = f.journal.read(f.job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Example legacy completion")
        self.assertIsNone(checkpoint["claude_material_notice"])
        before = f.snapshot()
        with self.assertRaises(StateError):
            f.journal.record_claude_completion(
                f.job.job_id,
                f.token,
                f.binding.session_id,
                "Example legacy completion",
                cwd=f.root,
                material_notice="",
            )
        self.assertEqual(f.snapshot(), before)

    def test_migration_fault_restores_schema_and_all_saved_evidence(self) -> None:
        self.legacy()
        before = self.fixture.snapshot()
        original = migrations._execute_migration_script

        def fail(connection, script):
            if script == migrations.MIGRATION_53:
                raise RuntimeError("example schema53 fault")
            original(connection, script)

        with patch.object(migrations, "_execute_migration_script", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "schema53 fault"):
                migrations.migrate_database(self.fixture.path)
        self.assertEqual(self.fixture.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
